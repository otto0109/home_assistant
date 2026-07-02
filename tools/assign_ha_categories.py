#!/usr/bin/env python3
"""Weist HA-Kategorien aus organize/ha_categories.yaml zu (UI-Gruppierung).

Voraussetzung:
  pip install websocket-client pyyaml

Aufruf:
  HA_URL=http://homeassistant.local:8123 \\
  HA_TOKEN=<long-lived-access-token> \\
  python3 tools/assign_ha_categories.py

Token: Profil → Sicherheit → Long-Lived Access Tokens
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Bitte installieren: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

try:
    import websocket
except ImportError:
    print("Bitte installieren: pip install websocket-client", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES_FILE = ROOT / "organize" / "ha_categories.yaml"

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# HA category scopes (Einstellungen-Tabellen)
SCOPES = {
    "automations": "automation",
    "scripts": "script",
    "helpers": "helper",
}


class HAWebSocket:
    def __init__(self, url: str, token: str) -> None:
        ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._pending: dict[int, object] = {}
        msg = json.loads(self._ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Unerwartete WS-Nachricht: {msg}")
        self._send({"type": "auth", "access_token": token})
        auth = json.loads(self._ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"Auth fehlgeschlagen: {auth}")

    def _send(self, payload: dict) -> int:
        msg_id = payload.get("id") or int(uuid.uuid4().hex[:8], 16)
        payload["id"] = msg_id
        self._ws.send(json.dumps(payload))
        return msg_id

    def call(self, msg_type: str, **kwargs) -> dict:
        msg_id = self._send({"type": msg_type, **kwargs})
        while True:
            raw = json.loads(self._ws.recv())
            if raw.get("id") == msg_id:
                if not raw.get("success", True):
                    raise RuntimeError(f"{msg_type} fehlgeschlagen: {raw}")
                return raw.get("result", raw)
            if raw.get("type") == "result" and raw.get("id") == msg_id:
                if not raw.get("success", True):
                    raise RuntimeError(f"{msg_type} fehlgeschlagen: {raw}")
                return raw.get("result", raw)

    def close(self) -> None:
        self._ws.close()


def load_config() -> dict:
    with open(CATEGORIES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_categories(ws: HAWebSocket, scope: str) -> dict[str, str]:
    result = ws.call("config/category_registry/list", scope=scope)
    entries = result if isinstance(result, list) else result.get("categories", result)
    by_name: dict[str, str] = {}
    for cat in entries or []:
        name = cat.get("name")
        cat_id = cat.get("category_id") or cat.get("id")
        if name and cat_id:
            by_name[name] = cat_id
    return by_name


def ensure_category(ws: HAWebSocket, scope: str, name: str, icon: str, cache: dict[str, str]) -> str:
    if name in cache:
        return cache[name]
    created = ws.call(
        "config/category_registry/create",
        scope=scope,
        name=name,
        icon=icon,
    )
    cat_id = created.get("category_id") or created.get("id")
    if not cat_id:
        raise RuntimeError(f"Kategorie konnte nicht erstellt werden: {created}")
    cache[name] = cat_id
    print(f"  + Kategorie '{name}' ({scope})")
    return cat_id


def build_automation_entity_map(ws: HAWebSocket) -> dict[str, str]:
    entities = ws.call("config/entity_registry/list")
    mapping: dict[str, str] = {}
    for ent in entities:
        if ent.get("entity_id", "").startswith("automation."):
            uid = ent.get("unique_id")
            if uid is not None:
                mapping[str(uid)] = ent["entity_id"]
    return mapping


def assign_entity_category(ws: HAWebSocket, entity_id: str, category_id: str) -> None:
    ws.call(
        "config/entity_registry/update",
        entity_id=entity_id,
        category=category_id,
    )


def main() -> int:
    if not HA_TOKEN:
        print("HA_TOKEN fehlt (Long-Lived Access Token).", file=sys.stderr)
        return 1
    if not CATEGORIES_FILE.exists():
        print(f"Datei fehlt: {CATEGORIES_FILE}", file=sys.stderr)
        return 1

    config = load_config()
    ws = HAWebSocket(HA_URL, HA_TOKEN)

    try:
        automation_map = build_automation_entity_map(ws)
        print("Automation-Entities:", len(automation_map))

        for section, scope in SCOPES.items():
            groups = config.get(section) or {}
            if not groups:
                continue
            print(f"\n=== {section} ({scope}) ===")
            cat_cache = list_categories(ws, scope)

            for cat_name, spec in groups.items():
                icon = spec.get("icon", "mdi:folder")
                cat_id = ensure_category(ws, scope, cat_name, icon, cat_cache)

                if section == "automations":
                    for auto_id in spec.get("ids", []):
                        eid = automation_map.get(str(auto_id))
                        if not eid:
                            print(f"  ! Automation-ID nicht gefunden: {auto_id}")
                            continue
                        assign_entity_category(ws, eid, cat_id)
                        print(f"  → {eid}")
                else:
                    for eid in spec.get("entity_ids", []):
                        assign_entity_category(ws, eid, cat_id)
                        print(f"  → {eid}")

        print("\nFertig. In HA: Einstellungen → Automationen & Szenen → Filter → Kategorie")
    finally:
        ws.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
