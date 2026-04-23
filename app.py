"""
CloudStream REST API — Flask on Azure App Service
COM682 Coursework 2 — Meharjot Singh B00963621

Ported from Azure Functions to Flask after encountering persistent
Linux Consumption startup issues on the Azure for Students subscription.
Code is otherwise identical — same endpoints, same auth, same managed
identity approach to Cosmos + Blob Storage.
"""

import os
import uuid
import base64
import datetime as dt
import logging
from functools import wraps

from flask import Flask, request, jsonify
import bcrypt
import jwt
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.cosmos import CosmosClient

# ---------------------------------------------------------------------------
# App + configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cloudstream")

STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT_NAME"]
COSMOS_ENDPOINT = os.environ["COSMOS_ENDPOINT"]
COSMOS_DATABASE = os.environ["COSMOS_DATABASE"]
MEDIA_CONTAINER = os.environ.get("BLOB_CONTAINER_MEDIA", "media")
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_EXPIRY_HOURS = int(os.environ.get("JWT_EXPIRY_HOURS", "24"))

# ---------------------------------------------------------------------------
# Azure client singletons
# ---------------------------------------------------------------------------
_credential = None
_blob_service = None
_cosmos_client = None


def credential():
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def blob_service():
    global _blob_service
    if _blob_service is None:
        url = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"
        _blob_service = BlobServiceClient(account_url=url, credential=credential())
    return _blob_service


def cosmos_client():
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
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


@app.route("/api/<path:any>", methods=["OPTIONS"])
def cors_preflight(any):
    return ("", 204)


def err(msg, status=400):
    return jsonify({"error": msg}), status


def issue_token(user_id, email):
    payload = {
        "sub": user_id,
        "email": email,
        "iat": dt.datetime.now(dt.timezone.utc),
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return err("Unauthorized — missing token", 401)
        try:
            claims = jwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"])
        except jwt.PyJWTError:
            return err("Unauthorized — invalid token", 401)
        request.claims = claims
        return f(*args, **kwargs)
    return wrapper


def find_media_by_id(media_id):
    rows = list(media_container().query_items(
        query="SELECT * FROM c WHERE c.mediaId = @mid",
        parameters=[{"name": "@mid", "value": media_id}],
        enable_cross_partition_query=True,
    ))
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({
        "service": "CloudStream API",
        "version": "1.0",
        "docs": "See /api/health",
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "cloudstream",
        "time": dt.datetime.utcnow().isoformat(),
    })


@app.route("/api/users/register", methods=["POST"])
def register():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    display_name = body.get("displayName") or email.split("@")[0]

    if not email or "@" not in email:
        return err("Valid email required")
    if len(password) < 8:
        return err("Password must be at least 8 characters")

    existing = list(users_container().query_items(
        query="SELECT * FROM c WHERE c.email = @email",
        parameters=[{"name": "@email", "value": email}],
        enable_cross_partition_query=True,
    ))
    if existing:
        return err("User already exists", 409)

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
    return jsonify({
        "userId": user_id, "email": email,
        "displayName": display_name, "token": token,
    }), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    rows = list(users_container().query_items(
        query="SELECT * FROM c WHERE c.email = @email",
        parameters=[{"name": "@email", "value": email}],
        enable_cross_partition_query=True,
    ))
    if not rows:
        return err("Invalid credentials", 401)
    user = rows[0]
    if not bcrypt.checkpw(password.encode(), user["passwordHash"].encode()):
        return err("Invalid credentials", 401)

    token = issue_token(user["userId"], user["email"])
    return jsonify({
        "userId": user["userId"], "email": user["email"],
        "displayName": user.get("displayName"), "token": token,
    })


@app.route("/api/media", methods=["POST"])
@require_auth
def create_media():
    body = request.get_json(silent=True) or {}
    title = body.get("title")
    media_type = body.get("mediaType", "image")
    file_name = body.get("fileName", "file.bin")
    file_b64 = body.get("fileBase64")
    content_type = body.get("contentType", "application/octet-stream")

    if not title or not file_b64:
        return err("title and fileBase64 are required")

    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception:
        return err("fileBase64 is not valid base64")

    user_id = request.claims["sub"]
    media_id = str(uuid.uuid4())
    blob_name = f"{user_id}/{media_id}/{file_name}"

    try:
        bc = blob_service().get_blob_client(container=MEDIA_CONTAINER, blob=blob_name)
        bc.upload_blob(
            file_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        file_url = bc.url
    except Exception as e:
        logger.exception("Blob upload failed")
        return err(f"Upload failed: {e}", 500)

    doc = {
        "id": media_id, "mediaId": media_id, "userId": user_id,
        "title": title, "description": body.get("description", ""),
        "mediaType": media_type, "fileUrl": file_url,
        "fileName": file_name, "fileSize": len(file_bytes),
        "contentType": content_type, "tags": body.get("tags", []),
        "status": "active",
        "uploadDate": dt.datetime.utcnow().isoformat(),
    }
    media_container().create_item(body=doc)
    return jsonify(doc), 201


@app.route("/api/media", methods=["GET"])
def list_media():
    user_id = request.args.get("userId")
    if user_id:
        items = list(media_container().query_items(
            query="SELECT * FROM c WHERE c.userId = @uid AND c.status = 'active' ORDER BY c.uploadDate DESC",
            parameters=[{"name": "@uid", "value": user_id}],
            partition_key=user_id,
        ))
    else:
        items = list(media_container().query_items(
            query="SELECT * FROM c WHERE c.status = 'active' ORDER BY c.uploadDate DESC",
            enable_cross_partition_query=True,
        ))
    return jsonify({"count": len(items), "items": items})


@app.route("/api/media/<media_id>", methods=["GET"])
def get_media(media_id):
    doc = find_media_by_id(media_id)
    if not doc or doc.get("status") == "deleted":
        return err("Media not found", 404)
    return jsonify(doc)


@app.route("/api/media/<media_id>", methods=["PUT"])
@require_auth
def update_media(media_id):
    body = request.get_json(silent=True) or {}
    user_id = request.claims["sub"]
    logger.info(f"update_media: id={media_id} userId={user_id}")

    doc = find_media_by_id(media_id)
    if not doc:
        return err("Media not found", 404)
    if doc.get("userId") != user_id:
        return err("Not owned by you", 403)

    for field in ("title", "description", "tags"):
        if field in body:
            doc[field] = body[field]
    doc["updatedAt"] = dt.datetime.utcnow().isoformat()

    result = media_container().upsert_item(body=doc)
    return jsonify(result)


@app.route("/api/media/<media_id>", methods=["DELETE"])
@require_auth
def delete_media(media_id):
    user_id = request.claims["sub"]
    logger.info(f"delete_media: id={media_id} userId={user_id}")

    doc = find_media_by_id(media_id)
    if not doc:
        return err("Media not found", 404)
    if doc.get("userId") != user_id:
        return err("Not owned by you", 403)

    # Delete blob
    file_url = doc.get("fileUrl", "")
    try:
        blob_path = file_url.split(f"/{MEDIA_CONTAINER}/", 1)[-1]
        blob_service().get_blob_client(container=MEDIA_CONTAINER, blob=blob_path).delete_blob()
    except Exception as e:
        logger.warning(f"Blob delete failed (continuing): {e}")

    # Soft-delete metadata
    doc["status"] = "deleted"
    doc["deletedAt"] = dt.datetime.utcnow().isoformat()
    media_container().upsert_item(body=doc)
    return jsonify({"deleted": media_id})


# ---------------------------------------------------------------------------
# Entrypoint for local dev. In Azure, gunicorn calls `app` directly.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
