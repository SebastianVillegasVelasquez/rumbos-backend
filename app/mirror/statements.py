"""The signed statement that tells the plugin an exam definition.

Claims (JWS payload, ES256, header `kid`, signed at send time like every
statement of the outbox):

    iss, aud ("moodle"), iat
    jti            a fixed id of (tenant, form, definition_hash): the plugin
                   accepts each once, so a resent definition changes nothing
    kind           "definition"
    schema         1
    tenant, form_id, content_version, definition_hash
    definition     the exam (`app.mirror.definition.build_definition`)

There is no `exp`: a row may wait in the outbox for as long as the plugin is
down, and is re-signed with the key current at the time it is sent. `iat` is
when it was queued, not when it was signed, so a receiver must not reject a
statement for being old.
"""

import uuid
from typing import Any

from app.passback.statement import AUDIENCE

# Namespace of the ids of definition statements; never changes.
DEFINITION_NAMESPACE = uuid.UUID("3b1d1b0e-6a5e-4d2f-9a53-6f3a2f7a8c11")
SCHEMA = 1


def definition_jti(
    tenant_id: uuid.UUID, form_id: str, definition_hash: str
) -> uuid.UUID:
    return uuid.uuid5(DEFINITION_NAMESPACE, f"{tenant_id}|{form_id}|{definition_hash}")


def definition_claims(
    *,
    tenant_id: uuid.UUID,
    form_id: str,
    content_version: str,
    definition_hash: str,
    definition: dict[str, Any],
    issuer: str,
    issued_at: int,
) -> dict[str, Any]:
    return {
        "iss": issuer,
        "aud": AUDIENCE,
        "iat": issued_at,
        "jti": str(definition_jti(tenant_id, form_id, definition_hash)),
        "kind": "definition",
        "schema": SCHEMA,
        "tenant": str(tenant_id),
        "form_id": form_id,
        "content_version": content_version,
        "definition_hash": definition_hash,
        "definition": definition,
    }
