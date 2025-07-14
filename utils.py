#!/usr/bin/env python3
from __future__ import annotations
import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

try:
    import google.generativeai as genai  # type: ignore
except ImportError:
    sys.exit("google-generativeai not installed. Run: pip install google-generativeai")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Summarise Cardano governance proposals with Gemini",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--since-proposal", help="Skip up to & incl. GAID (tx_hash#index)")
    g.add_argument("--since-date", help="Skip proposals announced on/before YYYY-MM-DD")
    p.add_argument("--max", type=int, default=None, help="Process at most N proposals")
    p.add_argument("--page-size", type=int, default=100, help="Koios page size (limit)")
    p.add_argument("--model", default="gemini-1.5-flash", help="Gemini model ID to use")
    p.add_argument("--out-dir", default="summaries", help="Directory to write GAID.md files")
    p.add_argument(
        "--base-url",
        default=os.getenv("KOIOS_BASE_URL", "https://api.koios.rest/api/v1"),
        help="Koios REST base URL (…/api/v1)",
    )
    p.add_argument("--verbose", action="store_true", help="Verbose HTTP logging")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Koios helpers
# ---------------------------------------------------------------------------

def _koios_get(base_url: str, path: str, params: Dict[str, Any], *, verbose: bool = False) -> Any:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    token = os.getenv("KOIOS_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    backoff = 1.0
    while True:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if verbose:
            print("GET", resp.url, "→", resp.status_code)
        if resp.status_code == 429:
            resp.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        resp.raise_for_status()
        return resp.json()


def list_proposals(
    base_url: str,
    page_size: int,
    after_gaid: Optional[str] = None,
    after_date: Optional[str] = None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {}
    if after_date:
        # Koios API uses block_time for filtering
        params["block_time"] = f"gt.{after_date}"

    proposals: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page_params = {**params, "limit": page_size, "offset": offset}
        try:
            chunk = _koios_get(base_url, "proposal_list", page_params, verbose=verbose)
        except requests.HTTPError as exc:
            if after_date and exc.response.status_code == 400:
                # If block_time filter fails, remove it and get all
                params.pop("block_time", None)
                continue
            raise
        if not chunk:
            break
        for prop in chunk:
            gaid = to_gaid(prop)
            if not gaid:
                continue
            if after_gaid and gaid <= after_gaid:
                continue
            if after_date and not block_time_passes(prop, after_date):
                continue
            proposals.append(prop)
        if len(chunk) < page_size:
            break
        offset += page_size
    return proposals


def block_time_passes(prop: Dict[str, Any], after_timestamp: str) -> bool:
    """Check if proposal's block_time is after the given timestamp"""
    block_time = prop.get("block_time")
    if block_time is None:
        return True  # Include if no block_time
    try:
        return int(block_time) > int(after_timestamp)
    except (ValueError, TypeError):
        return True  # Include if can't parse

# ---------------------------------------------------------------------------
# GAID util
# ---------------------------------------------------------------------------

def to_gaid_components(prop: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    tx_hash = prop.get("proposal_tx_hash") or prop.get("tx_hash") or prop.get("proposal_hash")
    idx = prop.get("proposal_index") or prop.get("gov_action_index") or prop.get("index")
    if tx_hash is None:
        return None
    if idx is None:
        idx = 0
    return tx_hash, int(idx)


def to_gaid(prop: Dict[str, Any]) -> Optional[str]:
    comps = to_gaid_components(prop)
    return None if comps is None else f"{comps[0]}#{comps[1]}"

# ---------------------------------------------------------------------------
# Metadata fetch (safe)
# ---------------------------------------------------------------------------

def fetch_meta(url: str, expected_hash: Optional[str] = None, *, verbose: bool = False) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(url, timeout=10, headers={"Accept": "application/json"})
        resp.raise_for_status()
        if "application/json" not in resp.headers.get("Content-Type", ""):
            if verbose:
                print("Rejected metadata: not JSON")
            return None
        if len(resp.content) > 1000_000:
            if verbose:
                print("Rejected metadata: too large")
            return None
        if expected_hash:
            calc = hashlib.sha256(resp.content).hexdigest()
            if calc.lower() != expected_hash.lower():
                if verbose:
                    print("Metadata hash mismatch")
                return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print("Metadata fetch failed:", exc)
        return None

# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------

def init_gemini(model_name: str) -> genai.GenerativeModel:  # type: ignore[name-defined]
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY not set")
    genai.configure(api_key=key)
    return genai.GenerativeModel(model_name)


def summarise(model: genai.GenerativeModel, proposal: Dict[str, Any]) -> str:
    prompt = (
        "You are an expert Cardano governance analyst. Given JSON metadata of an on-chain governance proposal, produce:\n"
        "1. A concise 2–3 sentence summary.\n2. 3–5 bullet insights (impact, pros/cons, contentious points).\n\n"
        "Proposal metadata (JSON):\n" + json.dumps(proposal, ensure_ascii=False, indent=2)
    )
    return model.generate_content(prompt).text.strip()

# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def link_templates(base_url: str) -> Dict[str, str]:
    gov = "https://preview.gov.tools/outcomes/governance_actions" if "preview" in base_url else "https://gov.tools/outcomes/governance_actions"
    return {
        "govtool": gov + "/{gaid}",
        "adastat": "https://adastat.net/governances/{ada_id}",
    }


def lovelace_to_ada(value: str | int | None) -> str:
    if value is None:
        return "?"
    # Skip placeholder values like "string"
    if isinstance(value, str) and value.lower() == "string":
        return "?"
    try:
        return f"{int(value) / 1_000_000:,.0f} ₳"
    except (ValueError, TypeError):
        return str(value)


def pick_title(prop: Dict[str, Any]) -> str:
    meta_json = prop.get("meta_json") or {}
    body = meta_json.get("body") or {}
    return (
        body.get("title")
        or prop.get("title")
        or f"Governance Action: {prop.get('proposal_type')}"
        or prop.get("proposal_description", {}).get("tag")
        or "Governance Action"
    )