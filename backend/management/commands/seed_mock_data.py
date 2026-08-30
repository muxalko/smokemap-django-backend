from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from backend.models import Address, Category, Location, Place, Tag
from backend.tagging import normalize_tag_text


MOCK_PLACES = (
    {
        "name": "Mock Capitol Patio",
        "category": "Outdoors",
        "description": "Fictional outdoor place for local map testing.",
        "address": "100 Mock Capitol Way, Washington, DC",
        "longitude": -77.0091,
        "latitude": 38.8899,
        "tags": ("patio", "mock-data"),
        "website": "https://example.com/mock-capitol-patio",
    },
    {
        "name": "Mock Dupont Lounge",
        "category": "Indoors",
        "description": "Fictional indoor place for local map testing.",
        "address": "200 Mock Dupont Circle, Washington, DC",
        "longitude": -77.0434,
        "latitude": 38.9096,
        "tags": ("lounge", "mock-data"),
        "website": "https://example.com/mock-dupont-lounge",
    },
    {
        "name": "Mock Georgetown Rooftop",
        "category": "Rooftop",
        "description": "Fictional rooftop place for local map testing.",
        "address": "300 Mock Waterfront Street, Washington, DC",
        "longitude": -77.0641,
        "latitude": 38.9052,
        "tags": ("rooftop", "mock-data"),
        "website": "https://example.com/mock-georgetown-rooftop",
    },
)


class Command(BaseCommand):
    help = "Seed deterministic development-only places for local map testing."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Mock data can only be seeded when DEBUG is enabled.")

        created_places = 0

        with transaction.atomic():
            for mock_place in MOCK_PLACES:
                category = self._category(mock_place["category"])
                point = Point(
                    mock_place["longitude"],
                    mock_place["latitude"],
                    srid=4326,
                )
                address = self._address(mock_place["address"], point)
                place, created = Place.objects.update_or_create(
                    name=mock_place["name"],
                    defaults={
                        "category": category,
                        "description": mock_place["description"],
                        "address": address,
                        "website": mock_place["website"],
                    },
                )
                created_places += int(created)

                tags = []
                for tag_name in mock_place["tags"]:
                    normalized = normalize_tag_text(tag_name)
                    tag, _created = Tag.objects.get_or_create(
                        canonical=normalized.canonical,
                        defaults={
                            "name": normalized.display,
                            "is_public": True,
                        },
                    )
                    if not tag.is_public:
                        tag.is_public = True
                        tag.save(update_fields=("is_public",))
                    tags.append(tag)
                place.tags.set(tags)

                Location.objects.update_or_create(
                    place_id=str(place.pk),
                    defaults={
                        "name": place.name,
                        "category": category.pk,
                        "info": place.description or "",
                        "address": address.addressString,
                        "tags": ",".join(mock_place["tags"]),
                        "geom": point,
                    },
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(MOCK_PLACES)} mock places "
                f"({created_places} created, "
                f"{len(MOCK_PLACES) - created_places} updated)."
            )
        )

    def _category(self, name):
        try:
            return Category.objects.get(name=name)
        except Category.DoesNotExist as error:
            raise CommandError(
                f"Required category {name!r} is missing; apply migrations first."
            ) from error

    def _address(self, address_string, point):
        address = Address.objects.filter(addressString=address_string).first()
        if address is None:
            address = Address(addressString=address_string, location=point)
            address.save(omit_geocode=True)
            return address

        Address.objects.filter(pk=address.pk).update(location=point)
        address.location = point
        return address
