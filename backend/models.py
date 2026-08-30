from django.core.exceptions import ValidationError
from django.contrib.gis.db import models
from django.utils import timezone
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.utils.crypto import constant_time_compare
from .managers import CustomUserManager
# Create your models here.

# https://docs.djangoproject.com/en/5.0/topics/auth/customizing/#custom-users-and-proxy-models
# AbstractUser vs AbstractBaseUser
# The default user model in Django uses a username to uniquely identify a user during authentication. If you'd rather use an email address, you'll need to create a custom user model by either subclassing AbstractUser or AbstractBaseUser.

# Options:

# AbstractUser: Use this option if you are happy with the existing fields on the user model and just want to remove the username field.
# AbstractBaseUser: Use this option if you want to start from scratch by creating your own, completely new user model.

# This model behaves identically to the default user model, but you’ll be able to customize it in the future if the need arises:
# https://docs.djangoproject.com/en/5.0/topics/auth/customizing/#using-a-custom-user-model-when-starting-a-project
# class User(AbstractUser):
#     pass

# Created a new class called CustomUser that subclasses AbstractBaseUser
# Removed the username field
# Made the email field required and unique
# Set the USERNAME_FIELD -- which defines the unique identifier for the User model -- to email
# Specified that all objects for the class come from the CustomUserManager
class CustomUser(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=25, default='visitor')
    image = models.CharField(max_length=255, default='/guest.svg')
    email = models.EmailField(unique=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def __str__(self):
        return self.email 


class Category(models.Model):
    slug = models.SlugField(unique=True, max_length=50)
    name = models.CharField(unique=True, max_length=25)
    description = models.CharField(null=True, max_length=255)

    class Meta:
        verbose_name_plural = 'Categories'

    def clean(self):
        super().clean()
        self._validate_immutable_slug()

    def save(self, *args, **kwargs):
        self._validate_immutable_slug()
        return super().save(*args, **kwargs)

    def _validate_immutable_slug(self):
        if not self.pk:
            return
        issued_slug = (
            type(self).objects.filter(pk=self.pk).values_list("slug", flat=True).first()
        )
        if issued_slug is not None and self.slug != issued_slug:
            raise ValidationError({"slug": "An issued category slug is immutable."})

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(unique=True, max_length=50)
    canonical = models.CharField(unique=True, max_length=50, editable=False)
    is_public = models.BooleanField(default=False)
    # the tag belongs to a category
    # category = models.ForeignKey(Category, blank=True, on_delete=models.PROTECT)

    class Meta:
        verbose_name_plural = 'Tags'

    def save(self, *args, **kwargs):
        from .tagging import normalize_tag_text

        normalized = normalize_tag_text(self.name)
        self.name = normalized.display
        self.canonical = normalized.canonical
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                "name",
                "canonical",
            }
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Address(models.Model):
    # This is an optional, human-supplied label. The submitted point is authoritative.
    addressString = models.CharField(blank=True, null=True, max_length=255)
    location = models.PointField(srid=4326)

    def __str__(self):
        return "{} ({},{})".format(
            self.addressString or "", self.location[0], self.location[1]
        )


def get_tags_default():
    """Retained for the serialized default in applied migration 0001."""
    return []


class Request(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending"
        WITHDRAWN = "withdrawn", "Withdrawn"
        EXPIRED = "expired", "Expired"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    name = models.CharField(max_length=100)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    description = models.CharField(blank=True, null=True, max_length=255)
    address = models.ForeignKey(Address, on_delete=models.PROTECT)
    tags = models.ManyToManyField(
        Tag,
        related_name="requests",
        through="RequestTag",
    )
    website = models.CharField(max_length=255, null=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)
    date_approved = models.DateTimeField(blank=True, null=True)
    approved = models.BooleanField(auto_created=True, default=False)
    approved_comment = models.TextField(blank=True, null=True)
    approved_by = models.CharField(
        auto_created=True, blank=True, null=True, max_length=100)
    requested_by = models.CharField(max_length=255, null=True)
    state = models.CharField(
        choices=State.choices,
        default=State.DRAFT,
        max_length=16,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submissions",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_submissions",
    )
    # images_set_id = models.CharField(blank=True, null=True, max_length=255)
    # the item belongs to one category
    # if having a category is required please remove blank=True
    # category = models.ForeignKey(Category, blank=True)
    # category = models.ForeignKey(Category, related_name='requests', on_delete=models.DO_NOTHING, blank=True, null=True)

    class Meta:
        ordering = ['-date_created']
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    state__in=[
                        "draft",
                        "pending",
                        "withdrawn",
                        "expired",
                        "approved",
                        "rejected",
                    ]
                ),
                name="request_valid_lifecycle_state",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(state="approved", approved=True)
                    | (~models.Q(state="approved") & models.Q(approved=False))
                ),
                name="request_state_legacy_approved_consistent",
            ),
        ]

    def __str__(self):
        return "{}: {} - {}, {}".format(self.date_created, self.name, self.description, self.address.addressString)


class RequestTag(models.Model):
    request = models.ForeignKey(
        Request,
        on_delete=models.CASCADE,
        related_name="request_tags",
    )
    tag = models.ForeignKey(
        Tag,
        on_delete=models.PROTECT,
        related_name="request_tags",
    )
    display = models.CharField(max_length=50)
    position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=("request", "tag"),
                name="unique_request_tag",
            ),
            models.UniqueConstraint(
                fields=("request", "position"),
                name="unique_request_tag_position",
            ),
            models.CheckConstraint(
                check=models.Q(position__gte=0, position__lt=10),
                name="request_tag_position_range",
            ),
            models.CheckConstraint(
                check=models.Q(display__regex=r"^.{3,50}$"),
                name="request_tag_display_length",
            ),
        ]


class SubmissionOperation(models.TextChoices):
    CREATE = "submission.create.v3", "Create submission"
    FINALIZE = "submission.finalize.v3", "Finalize submission"
    EXPIRE = "submission.expire.v3", "Expire submission"
    WITHDRAW = "submission.withdraw.v4", "Withdraw submission"
    APPROVE = "submission.approve.v4", "Approve submission"
    REJECT = "submission.reject.v4", "Reject submission"


class SubmissionIdempotency(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submission_idempotency_records",
    )
    operation = models.CharField(max_length=32, choices=SubmissionOperation.choices)
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64, editable=False)
    submission = models.ForeignKey(
        Request,
        on_delete=models.CASCADE,
        related_name="idempotency_records",
    )
    original_result = models.JSONField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("actor", "operation", "key"),
                name="unique_submission_idempotency_scope",
            ),
            models.CheckConstraint(
                check=~models.Q(key=""),
                name="submission_idempotency_key_not_empty",
            ),
        ]


class SubmissionLifecycleEvent(models.Model):
    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"

    submission = models.ForeignKey(
        Request,
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submission_lifecycle_events",
    )
    operation = models.CharField(max_length=32, choices=SubmissionOperation.choices)
    from_state = models.CharField(
        blank=True,
        null=True,
        max_length=16,
        choices=Request.State.choices,
    )
    to_state = models.CharField(max_length=16, choices=Request.State.choices)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    idempotency = models.OneToOneField(
        SubmissionIdempotency,
        on_delete=models.CASCADE,
        related_name="lifecycle_event",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(
                        operation=SubmissionOperation.CREATE,
                        from_state__isnull=True,
                        to_state=Request.State.DRAFT,
                    )
                    | models.Q(
                        operation=SubmissionOperation.FINALIZE,
                        from_state=Request.State.DRAFT,
                        to_state=Request.State.PENDING,
                    )
                    | models.Q(
                        operation=SubmissionOperation.EXPIRE,
                        from_state=Request.State.DRAFT,
                        to_state=Request.State.EXPIRED,
                    )
                    | models.Q(
                        operation=SubmissionOperation.WITHDRAW,
                        from_state__in=[Request.State.DRAFT, Request.State.PENDING],
                        to_state=Request.State.WITHDRAWN,
                    )
                    | models.Q(
                        operation=SubmissionOperation.APPROVE,
                        from_state=Request.State.PENDING,
                        to_state=Request.State.APPROVED,
                    )
                    | models.Q(
                        operation=SubmissionOperation.REJECT,
                        from_state=Request.State.PENDING,
                        to_state=Request.State.REJECTED,
                    )
                ),
                name="submission_event_valid_transition",
            ),
        ]


class ModerationAudit(models.Model):
    class Action(models.TextChoices):
        APPROVE = "approve", "Approve"
        HARD_DELETE = "hard_delete", "Hard delete"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="moderation_audits",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    target_type = models.CharField(max_length=32)
    target_id = models.PositiveBigIntegerField()
    outcome = models.CharField(max_length=32, default="succeeded")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return "{}:{}:{}".format(self.action, self.target_type, self.target_id)


class RefreshTokenFamily(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="refresh_token_families",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(blank=True, null=True)
    compromised_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return "refresh-family:{}:{}".format(self.pk, self.user_id)


class RefreshTokenCredential(models.Model):
    family = models.ForeignKey(
        RefreshTokenFamily,
        on_delete=models.CASCADE,
        related_name="credentials",
    )
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    successor = models.OneToOneField(
        "self",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="predecessor",
    )

    def matches_digest(self, digest):
        return constant_time_compare(self.token_digest, digest)

    def __str__(self):
        return "refresh-credential:{}:{}".format(self.pk, self.family_id)

class Place(models.Model):
    name = models.CharField(max_length=255)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    description = models.TextField(max_length=255, null=True)
    address = models.ForeignKey(Address, on_delete=models.PROTECT)
    tags = models.ManyToManyField(Tag, related_name="places")
    # images_set_id = models.CharField(blank=True, null=True, max_length=255)
    website = models.CharField(max_length=255, null=True)
    class Meta:
        verbose_name_plural = 'Places'

    def __str__(self):
        return "{}".format(self.name)
        # return "{} ({},{})".format(self.name, self.address.location[0], self.address.location[1])

class Image(models.Model):
    set_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    url = models.CharField(max_length=255)
    metadata = models.JSONField(blank=True, null=True)
    request = models.ForeignKey(Request, on_delete=models.DO_NOTHING, null=True)
    place = models.ForeignKey(Place, on_delete=models.DO_NOTHING, null=True)
    class Meta:
        verbose_name_plural = 'Images'

    def __str__(self):
        return self.url
    

class Location(models.Model):
    place_id = models.CharField(max_length=10)
    name = models.CharField(max_length=128)
    category = models.IntegerField()
    info = models.CharField(max_length=254,default="")
    address = models.CharField(max_length=254,default="")
    tags = models.CharField(max_length=254,default="")
    geom = models.PointField(srid=4326)
    
    class Meta:
        verbose_name_plural = 'Locations'

    def __str__(self):
        # return "{}".format(self.name)
        return "{},{},{},({}),[{}]".format(self.name, self.category, self.address, self.geom, self.tags)
