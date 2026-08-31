from contextlib import closing

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings


class StorageObjectNotFound(Exception):
    pass


class StorageOperationError(Exception):
    pass


class _SafeStreamingBody:
    def __init__(self, body):
        self.body = body

    def read(self, amount=-1):
        try:
            return self.body.read(amount)
        except (BotoCoreError, OSError) as error:
            raise StorageOperationError("object stream could not be read") from error

    def close(self):
        try:
            self.body.close()
        except (BotoCoreError, OSError):
            pass


def _is_not_found(error):
    response = getattr(error, "response", {})
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


class S3MediaStorage:
    """Narrow private-object boundary. Callers always supply an exact bucket/key."""

    def __init__(self, client=None):
        if client is not None:
            self.client = client
            return
        try:
            self.client = boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_S3_REGION_NAME or None,
                endpoint_url=settings.AWS_S3_ENDPOINT_URL or None,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": settings.AWS_S3_ADDRESSING_STYLE},
                ),
            )
        except (BotoCoreError, ValueError, TypeError) as error:
            raise StorageOperationError("private media storage could not be configured") from error

    def issue_upload(self, *, bucket, key, mime_type, maximum_size, expires_in):
        try:
            return self.client.generate_presigned_post(
                Bucket=bucket,
                Key=key,
                Fields={"Content-Type": mime_type},
                Conditions=[
                    {"key": key},
                    {"Content-Type": mime_type},
                    ["content-length-range", 1, maximum_size],
                ],
                ExpiresIn=expires_in,
            )
        except (BotoCoreError, ClientError, ValueError) as error:
            raise StorageOperationError("upload authorization could not be issued") from error

    def open_object(self, *, bucket, key):
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                raise StorageObjectNotFound("object is absent") from error
            raise StorageOperationError("object could not be read") from error
        except BotoCoreError as error:
            raise StorageOperationError("object could not be read") from error
        body = response.get("Body")
        if body is None:
            raise StorageOperationError("object response did not contain a body")
        return closing(_SafeStreamingBody(body))

    def seal_object(self, *, bucket, key, body, content_type, content_length):
        try:
            self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                ContentLength=content_length,
            )
        except (BotoCoreError, ClientError, OSError, ValueError) as error:
            raise StorageOperationError("verified object could not be sealed") from error

    def object_size(self, *, bucket, key):
        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                raise StorageObjectNotFound("object is absent") from error
            raise StorageOperationError("object metadata could not be read") from error
        except BotoCoreError as error:
            raise StorageOperationError("object metadata could not be read") from error
        size = response.get("ContentLength")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise StorageOperationError("object metadata was invalid")
        return size

    def delete_object(self, *, bucket, key):
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise StorageOperationError("object could not be deleted") from error

    def object_is_absent(self, *, bucket, key):
        try:
            self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                return True
            raise StorageOperationError("object absence could not be confirmed") from error
        except BotoCoreError as error:
            raise StorageOperationError("object absence could not be confirmed") from error
        return False


def configured_media_storage():
    return S3MediaStorage()
