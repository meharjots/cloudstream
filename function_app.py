"""
CloudStream REST API
COM682 Coursework 2 — Meharjot Singh B00963621

Python v2 model Azure Functions app exposing CRUD endpoints for a
cloud-native multimedia sharing platform.

Auth: JWT (HS256). Passwords stored as bcrypt hashes.
Storage: Azure Blob (media files) + Cosmos DB (metadata), both accessed
via DefaultAzureCredential — no connection strings in code.
"""

import azure.functions as func
import logging
import json
import os
import uuid
import base64
import datetime as dt
from typing import Any

import bcrypt
import jwt
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions

# ---------------------------------------------------------------------------
# App + configuration
# ---------------------------------------------------------------------------
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
logger = logging.getLogger("cloudstream")

STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT_NAME"]
COSMOS_ENDPOINT = os.environ["COSMOS_ENDPOINT"]
COSMOS_DATABASE = os.environ["COSMOS_DATABASE"]
MEDIA_CONTAINER = os.environ.get("BLOB_CONTAINER_MEDIA", "media")
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "24"))

# ---------------------------------------------------------------------------
# Azure client singletons (created on first request, reused afterwards)
# ---------------------------------------------------------------------------
_credential = None
_blob_service = None
_cosmos_client = None


def credential() -> DefaultAzureCredential:
    """Lazy-init Azure AD credential. Managed identity in Azure, az-login locally."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def blob_service() -> BlobServiceClient:
    global _blob_service
    if _blob_service is None:
        url = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"
        _blob_service = BlobServiceClient(account_url=url, credential=credential())
    return _blob_service


def cosmos_client() -> CosmosClient:
    global _cosmos_client
    if _cosmos_client is None:
        _cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential=credential())
    return _cosmos_client


def users_container():
    return cosmos_client().get_database_client(COSMOS_DATABASE).get_container_client("users")


def media_container():
    return cosmos_client().get_database_client(COSMOS_DATABASE).get_container_client("media")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def json_response(payload: Any, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload, default=str),
        status_code=status,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def error(msg: str, status: int = 400) -> func.HttpResponse:
    return json_response({"error": msg}, status)


def issue_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "iat": dt.datetime.now(dt.timezone.utc),
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_token(req: func.HttpRequest) -> dict | None:
    """Return decoded JWT payload or None if missing/invalid."""
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        return jwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def require_auth(req: func.HttpRequest) -> tuple[dict | None, func.HttpResponse | None]:
    """Returns (claims, None) on success or (None, error_response) on failure."""
    claims = verify_token(req)
    if not claims:
        return None, error("Unauthorized — missing or invalid token", 401)
    return claims, None


def find_media_by_id(media_id: str) -> dict | None:
    """Cross-partition lookup by mediaId. Avoids SDK partition_key header quirks."""
    rows = list(media_container().query_items(
        query="SELECT * FROM c WHERE c.mediaId = @mid",
        parameters=[{"name": "@mid", "value": media_id}],
        enable_cross_partition_query=True,
    ))
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return json_response({"status": "ok", "service": "cloudstream", "time": dt.datetime.utcnow().isoformat()})


# ---------------------------------------------------------------------------
# User registration + login
# ---------------------------------------------------------------------------
@app.route(route="users/register", methods=["POST"])
def register(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return error("Invalid JSON")

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    display_name = body.get("displayName") or email.split("@")[0]

    if not email or "@" not in email:
        return error("Valid email required")
    if len(password) < 8:
        return error("Password must be at least 8 characters")

    existing = list(users_container().query_items(
        query="SELECT * FROM c WHERE c.email = @email",
        parameters=[{"name": "@email", "value": email}],
        enable_cross_partition_query=True,
    ))
    if existing:
        return error("User already exists", 409)

    user_id = str(uuid.uuid4())
    user_doc = {
        "id": user_id,
        "userId": user_id,
        "email": email,
        "displayName": display_name,
        "passwordHash": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        "createdAt": dt.datetime.utcnow().isoformat(),
    }
    users_container().create_item(body=user_doc)

    token = issue_token(user_id, email)
    return json_response({"userId": user_id, "email": email, "displayName": display_name, "token": token}, 201)


@app.route(route="auth/login", methods=["POST"])
def login(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return error("Invalid JSON")

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    rows = list(users_container().query_items(
        query="SELECT * FROM c WHERE c.email = @email",
        parameters=[{"name": "@email", "value": email}],
        enable_cross_partition_query=True,
    ))
    if not rows:
        return error("Invalid credentials", 401)

    user = rows[0]
    if not bcrypt.checkpw(password.encode(), user["passwordHash"].encode()):
        return error("Invalid credentials", 401)

    token = issue_token(user["userId"], user["email"])
    return json_response({
        "userId": user["userId"], "email": user["email"],
        "displayName": user.get("displayName"), "token": token,
    })


# ---------------------------------------------------------------------------
# Media — CRUD
# ---------------------------------------------------------------------------
@app.route(route="media", methods=["POST"])
def create_media(req: func.HttpRequest) -> func.HttpResponse:
    """
    Upload a media file + metadata.

    Request body (JSON):
      {
        "title": "Beach Sunset",
        "description": "...",
        "mediaType": "image" | "video",
        "fileName": "sunset.jpg",
        "fileBase64": "<base64-encoded file bytes>",
        "contentType": "image/jpeg",
        "tags": ["beach", "sunset"]
      }
    """
    claims, err = require_auth(req)
    if err:
        return err

    try:
        body = req.get_json()
    except ValueError:
        return error("Invalid JSON")

    title = body.get("title")
    media_type = body.get("mediaType", "image")
    file_name = body.get("fileName", "file.bin")
    file_b64 = body.get("fileBase64")
    content_type = body.get("contentType", "application/octet-stream")

    if not title or not file_b64:
        return error("title and fileBase64 are required")

    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception:
        return error("fileBase64 is not valid base64")

    user_id = claims["sub"]
    media_id = str(uuid.uuid4())
    blob_name = f"{user_id}/{media_id}/{file_name}"

    # Upload to Blob
    try:
        blob_client = blob_service().get_blob_client(container=MEDIA_CONTAINER, blob=blob_name)
        blob_client.upload_blob(
            file_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        file_url = blob_client.url
    except Exception as e:
        logger.exception("Blob upload failed")
        return error(f"Upload failed: {e}", 500)

    # Persist metadata
    doc = {
        "id": media_id,
        "mediaId": media_id,
        "userId": user_id,
        "title": title,
        "description": body.get("description", ""),
        "mediaType": media_type,
        "fileUrl": file_url,
        "fileName": file_name,
        "fileSize": len(file_bytes),
        "contentType": content_type,
        "tags": body.get("tags", []),
        "status": "active",
        "uploadDate": dt.datetime.utcnow().isoformat(),
    }
    media_container().create_item(body=doc)
    return json_response(doc, 201)


@app.route(route="media", methods=["GET"])
def list_media(req: func.HttpRequest) -> func.HttpResponse:
    """
    List media. Optional ?userId=xxx filters to one user.
    """
    user_id = req.params.get("userId")
    if user_id:
        items = list(media_container().query_items(
            query="SELECT * FROM c WHERE c.userId = @uid ORDER BY c.uploadDate DESC",
            parameters=[{"name": "@uid", "value": user_id}],
            partition_key=user_id,
        ))
    else:
        items = list(media_container().query_items(
            query="SELECT * FROM c WHERE c.status = 'active' ORDER BY c.uploadDate DESC",
            enable_cross_partition_query=True,
        ))
    return json_response({"count": len(items), "items": items})


@app.route(route="media/{id}", methods=["GET"])
def get_media(req: func.HttpRequest) -> func.HttpResponse:
    media_id = req.route_params.get("id")
    doc = find_media_by_id(media_id)
    if not doc:
        return error("Media not found", 404)
    return json_response(doc)


@app.route(route="media/{id}", methods=["PUT"])
def update_media(req: func.HttpRequest) -> func.HttpResponse:
    claims, err = require_auth(req)
    if err:
        return err

    media_id = req.route_params.get("id")
    try:
        body = req.get_json()
    except ValueError:
        return error("Invalid JSON")

    user_id = claims["sub"]
    logger.info(f"update_media: id={media_id} userId={user_id} fields={list(body.keys())}")

    doc = find_media_by_id(media_id)
    if not doc:
        return error("Media not found", 404)
    if doc.get("userId") != user_id:
        return error("Not owned by you", 403)

    for field in ("title", "description", "tags"):
        if field in body:
            doc[field] = body[field]
    doc["updatedAt"] = dt.datetime.utcnow().isoformat()

    result = media_container().upsert_item(body=doc)
    logger.info(f"update_media: persisted _ts={result.get('_ts')}")
    return json_response(result)


@app.route(route="media/{id}", methods=["DELETE"])
def delete_media(req: func.HttpRequest) -> func.HttpResponse:
    claims, err = require_auth(req)
    if err:
        return err

    media_id = req.route_params.get("id")
    user_id = claims["sub"]
    logger.info(f"delete_media: id={media_id} userId={user_id}")

    doc = find_media_by_id(media_id)
    if not doc:
        return error("Media not found", 404)
    if doc.get("userId") != user_id:
        return error("Not owned by you", 403)

    # Delete blob immediately (this part works fine)
    file_url = doc.get("fileUrl", "")
    try:
        blob_path = file_url.split(f"/{MEDIA_CONTAINER}/", 1)[-1]
        blob_service().get_blob_client(container=MEDIA_CONTAINER, blob=blob_path).delete_blob()
    except Exception as e:
        logger.warning(f"Blob delete failed (continuing): {e}")

    # Soft-delete: mark as deleted in Cosmos via upsert (works reliably with RBAC)
    doc["status"] = "deleted"
    doc["deletedAt"] = dt.datetime.utcnow().isoformat()
    media_container().upsert_item(body=doc)

    logger.info(f"delete_media: soft-deleted {media_id}")
    return json_response({"deleted": media_id})