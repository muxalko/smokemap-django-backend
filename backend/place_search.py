import json
import unicodedata

from django.contrib.postgres.search import TrigramSimilarity
from django.db import connection, transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Lower

from backend.models import Place


SEARCH_QUERY_MIN_LENGTH = 2
SEARCH_QUERY_MAX_LENGTH = 100
SEARCH_DEFAULT_LIMIT = 10
SEARCH_RESULT_LIMIT = 20
SEARCH_FUZZY_THRESHOLD = 0.3


class PlaceSearchInputError(ValueError):
    def __init__(self, message, *, code="invalid_search"):
        super().__init__(message)
        self.code = code


def normalize_search_query(raw_query):
    if raw_query is None:
        raise PlaceSearchInputError("q is required")
    # PostgreSQL text cannot store NUL, so reject it before any database call.
    if "\x00" in raw_query:
        raise PlaceSearchInputError("q must not contain null characters")

    normalized = " ".join(unicodedata.normalize("NFKC", raw_query).split()).lower()
    if len(normalized) < SEARCH_QUERY_MIN_LENGTH:
        raise PlaceSearchInputError(
            f"q must contain at least {SEARCH_QUERY_MIN_LENGTH} characters"
        )
    if len(normalized) > SEARCH_QUERY_MAX_LENGTH:
        raise PlaceSearchInputError(
            f"q must contain at most {SEARCH_QUERY_MAX_LENGTH} characters"
        )
    return normalized


def parse_search_limit(raw_limit):
    if raw_limit in (None, ""):
        return SEARCH_DEFAULT_LIMIT
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as error:
        raise PlaceSearchInputError("limit must be an integer from 1 through 20") from error
    if not 1 <= limit <= SEARCH_RESULT_LIMIT:
        raise PlaceSearchInputError("limit must be an integer from 1 through 20")
    return limit


def place_search_queryset(normalized_query, *, fuzzy=True):
    """Build the ranked public-place query without evaluating it.

    Without extractable trigrams the fuzzy predicate cannot match anything and
    would force a sequential scan, so such queries search prefixes only.
    """
    normalized_name = Lower("name")
    candidates = Q(normalized_name__startswith=normalized_query)
    if fuzzy:
        candidates |= Q(normalized_name__trigram_similar=normalized_query)
    queryset = Place.objects.annotate(
        normalized_name=normalized_name,
        similarity=TrigramSimilarity(normalized_name, Value(normalized_query)),
    ).filter(candidates)
    return (
        queryset.annotate(
            match_rank=Case(
                When(normalized_name__startswith=normalized_query, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .select_related("address", "category")
        .order_by("match_rank", "-similarity", "normalized_name", "pk")
    )


def _configure_search_transaction(normalized_query):
    """Set the transaction-local fuzzy threshold and report whether pg_trgm
    extracts any trigram from the query (punctuation-only queries yield none)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('pg_trgm.similarity_threshold', %s, true), "
            "cardinality(show_trgm(%s)) > 0",
            [str(SEARCH_FUZZY_THRESHOLD), normalized_query],
        )
        return cursor.fetchone()[1]


def search_places(normalized_query, *, limit):
    """Evaluate a bounded query with an explicit, request-local fuzzy threshold."""
    with transaction.atomic():
        fuzzy = _configure_search_transaction(normalized_query)
        return list(place_search_queryset(normalized_query, fuzzy=fuzzy)[:limit])


def explain_place_search(normalized_query, *, limit=SEARCH_RESULT_LIMIT):
    """Capture the actual bounded ORM query's natural PostgreSQL plan."""
    with transaction.atomic():
        fuzzy = _configure_search_transaction(normalized_query)
        return json.loads(
            place_search_queryset(normalized_query, fuzzy=fuzzy)[:limit].explain(
                analyze=True,
                buffers=True,
                format="json",
            )
        )[0]


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)
