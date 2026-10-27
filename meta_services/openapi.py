"""OpenAPI description for the Meta operator API."""


def _schema(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema):
    return {"content": {"application/json": {"schema": _schema(schema)}}}


def _success(schema, description="Successful response"):
    return {"200": {"description": description, **_json(schema)}}


def _errors(*codes):
    descriptions = {
        "400": "Invalid request",
        "401": "Missing or invalid service bearer key",
        "403": "Invalid webhook signature or verification token",
        "404": "Resource not found",
        "409": "Idempotency key already used for another action or uncertain result",
        "413": "Request body too large",
        "423": "Outbound writes are disabled",
        "502": "Meta Graph request failed or write outcome is unknown",
    }
    return {
        code: {"description": descriptions[code], **_json("Error")}
        for code in codes
    }


def _bearer(operation):
    return {**operation, "security": [{"BearerAuth": []}]}


def _limit():
    return {
        "name": "limit", "in": "query", "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    }


def _after():
    return {"name": "after", "in": "query", "required": False,
            "schema": {"type": "string", "maxLength": 4096}}


def _idempotency():
    return {
        "name": "Idempotency-Key", "in": "header", "required": True,
        "schema": {"type": "string", "minLength": 8, "maxLength": 128,
                   "pattern": "^[A-Za-z0-9._:-]+$"},
        "description": "Reuse the same key after a timeout; use a new key for a new action.",
    }


OPENAPI_DOCUMENT = {
    "openapi": "3.1.0",
    "info": {
        "title": "Meta Services API",
        "version": "1.0.0",
        "description": "Local Facebook Page posts, comments, webhook, inbox, and review API.",
    },
    "servers": [{"url": "/"}],
    "components": {
        "securitySchemes": {
            "BearerAuth": {"type": "http", "scheme": "bearer",
                            "bearerFormat": "META_SERVICE_API_KEY"},
        },
        "schemas": {
            "Error": {
                "type": "object", "properties": {
                    "error": {"type": "string"}, "status": {"type": ["integer", "string", "null"]},
                    "code": {"type": ["integer", "null"]},
                    "outcome_unknown": {"type": "boolean"},
                }, "additionalProperties": True,
            },
            "Health": {
                "type": "object", "required": ["ok", "page_configured", "openrouter_configured",
                                                    "outbound_enabled", "pending_comments", "failed_comments"],
                "properties": {
                    "ok": {"type": "boolean"}, "page_configured": {"type": "boolean"},
                    "openrouter_configured": {"type": "boolean"},
                    "outbound_enabled": {"type": "boolean"},
                    "pending_comments": {"type": "integer"}, "failed_comments": {"type": "integer"},
                },
            },
            "GraphCollection": {
                "type": "object", "properties": {
                    "data": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                    "paging": {"type": "object", "additionalProperties": True},
                }, "additionalProperties": True,
            },
            "Comment": {"type": "object", "additionalProperties": True,
                         "properties": {"id": {"type": "string"}, "message": {"type": "string"},
                                        "post_id": {"type": "string"}}},
            "Inbox": {"type": "object", "properties": {
                "data": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            }},
            "Reviews": {"type": "object", "properties": {
                "data": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            }},
            "ReplyRequest": {"type": "object", "required": ["message"], "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 10000},
            }},
            "PostRequest": {"type": "object", "required": ["message"], "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 63000},
                "link": {"type": ["string", "null"], "format": "uri", "pattern": "^https://"},
            }},
            "WriteResult": {"type": "object", "required": ["id"], "properties": {
                "id": {"type": "string"}, "duplicate": {"type": "boolean"},
            }, "additionalProperties": True},
            "WebhookAccepted": {"type": "object", "required": ["ok", "queued", "outbound_enabled"],
                                 "properties": {"ok": {"type": "boolean"}, "queued": {"type": "integer"},
                                                "outbound_enabled": {"type": "boolean"}}},
        },
    },
    "paths": {
        "/health": {"get": {"operationId": "getHealth", "summary": "Service health",
                              "responses": _success("Health")}},
        "/webhooks/meta": {
            "get": {
                "operationId": "verifyMetaWebhook", "summary": "Meta webhook verification",
                "parameters": [
                    {"name": "hub.mode", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "hub.verify_token", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "hub.challenge", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Verification challenge accepted",
                                         "content": {"text/plain": {"schema": {"type": "string"}}}},
                              **_errors("403")},
            },
            "post": {
                "operationId": "receiveMetaWebhook", "summary": "Receive signed Page webhook",
                "parameters": [{"name": "X-Hub-Signature-256", "in": "header", "required": True,
                                "schema": {"type": "string", "pattern": "^sha256=[0-9a-fA-F]{64}$"}}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "additionalProperties": True,
                }}}},
                "responses": {**_success("WebhookAccepted", "Webhook verified and accepted"),
                              **_errors("400", "403", "413")},
            },
        },
        "/api/posts": {
            "get": _bearer({"operationId": "listPosts", "summary": "List Page posts",
                             "parameters": [_limit(), _after()],
                             "responses": {**_success("GraphCollection"), **_errors("400", "401", "502")}}),
            "post": _bearer({"operationId": "publishPost", "summary": "Publish a Page post",
                              "parameters": [_idempotency()],
                              "requestBody": {"required": True, "content": {"application/json": {
                                  "schema": _schema("PostRequest")}}},
                              "responses": {**_success("WriteResult"),
                                            **_errors("400", "401", "409", "423", "502")}}),
        },
        "/api/posts/{post_id}/comments": {"get": _bearer({
            "operationId": "listPostComments", "summary": "List comments on a Page post",
            "parameters": [{"name": "post_id", "in": "path", "required": True,
                            "schema": {"type": "string", "pattern": "^[0-9]+(?:_[0-9]+)*$"}},
                           _limit(), _after()],
            "responses": {**_success("GraphCollection"), **_errors("400", "401", "404", "502")},
        })},
        "/api/comments/{comment_id}": {"get": _bearer({
            "operationId": "getComment", "summary": "Get a comment belonging to this Page",
            "parameters": [{"name": "comment_id", "in": "path", "required": True,
                            "schema": {"type": "string", "pattern": "^[0-9]+(?:_[0-9]+)*$"}}],
            "responses": {**_success("Comment"), **_errors("400", "401", "404", "502")},
        })},
        "/api/comments/{comment_id}/reply": {"post": _bearer({
            "operationId": "replyToComment", "summary": "Reply to a Page comment",
            "parameters": [{"name": "comment_id", "in": "path", "required": True,
                            "schema": {"type": "string", "pattern": "^[0-9]+(?:_[0-9]+)*$"}},
                           _idempotency()],
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": _schema("ReplyRequest")}}},
            "responses": {**_success("WriteResult"),
                          **_errors("400", "401", "404", "409", "423", "502")},
        })},
        "/api/inbox": {"get": _bearer({
            "operationId": "listInbox", "summary": "List processed or queued comments",
            "parameters": [{**_limit(), "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}],
            "responses": {**_success("Inbox"), **_errors("400", "401")},
        })},
        "/api/reviews": {"get": _bearer({
            "operationId": "listReviews", "summary": "List drafts requiring staff review",
            "parameters": [{**_limit(), "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}],
            "responses": {**_success("Reviews"), **_errors("400", "401")},
        })},
    },
}
