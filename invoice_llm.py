import base64
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fitz
except Exception:  # pragma: no cover - optional runtime dependency
    fitz = None


def _load_project_env() -> None:
    root = Path(__file__).resolve().parent
    env_path = root / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        loaded = load_dotenv(env_path, override=False)
        if loaded:
            return
    except Exception:
        pass

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        if not value and key in os.environ and str(os.environ[key]).strip():
            continue
        value = re.sub(r"\s+#.*$", "", value)
        os.environ.setdefault(key, value)


_load_project_env()


INVOICE_LLM_SYSTEM_PROMPT = (
    "You are extracting line items from a scanned Egyptian supply invoice (Arabic + English, RTL table).\n"
    "Return JSON only. Copy printed values; never fix or compute numbers. Columns read right-to-left:\n"
    "م / اسم الصنف / الوحدة / الكمية / سعر الوحدة / الإجمالي. One output line per م. Each item cell repeats the\n"
    "description twice: merge into one line, English/model text in description_en. part_numbers: every P/N or model token\n"
    "exactly as printed (digits converted), never invented. Convert Arabic-Indic digits to 0-9 (the zero looks like a dot),\n"
    "remove thousands separators, and keep the raw printed strings in raw.qty / raw.unit_price / raw.line_total.\n"
    "Unreadable or blank => null plus a flag. Also return invoice_no, supply_order_no, customer, supplier,\n"
    "date (null if blank), printed_grand_total when shown."
)

INVOICE_LLM_PAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "page_no": {"type": "integer", "minimum": 1},
        "type": {"type": "string", "enum": ["items", "certificate", "other"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "contract_item_no": {"type": ["string", "null"]},
                    "description_ar": {"type": ["string", "null"]},
                    "description_en": {"type": ["string", "null"]},
                    "part_numbers": {"type": "array", "items": {"type": "string"}},
                    "unit": {"type": ["string", "null"]},
                    "qty": {"type": ["number", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "line_total": {"type": ["number", "null"]},
                    "raw": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "qty": {"type": ["string", "null"]},
                            "unit_price": {"type": ["string", "null"]},
                            "line_total": {"type": ["string", "null"]},
                        },
                        "required": ["qty", "unit_price", "line_total"],
                    },
                    "confidence": {"type": ["number", "null"]},
                    "flags": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "contract_item_no",
                    "description_ar",
                    "description_en",
                    "part_numbers",
                    "unit",
                    "qty",
                    "unit_price",
                    "line_total",
                    "raw",
                    "confidence",
                    "flags",
                ],
            },
        },
    },
    "required": ["page_no", "type", "lines"],
}

INVOICE_LLM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "invoice_no": {"type": ["string", "null"]},
                "supply_order_no": {"type": ["string", "null"]},
                "customer": {"type": ["string", "null"]},
                "supplier": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"]},
                "printed_grand_total": {"type": ["number", "null"]},
            },
            "required": ["invoice_no", "supply_order_no", "customer", "supplier", "date", "printed_grand_total"],
        },
        "pages": {
            "type": "array",
            "items": INVOICE_LLM_PAGE_SCHEMA,
        },
    },
    "required": ["document", "pages"],
}


def _gemini_compatible_schema(schema: Any) -> Any:
    """Gemini's JSON Schema support is narrower than OpenAI's. Strip unsupported keys and nullable unions.

    This keeps the system prompt strict enough for the JSON response while avoiding the API's validation trap
    around `additionalProperties` and `type: ["string", "null"]` payloads that raise HTTP 400s.
    """
    if isinstance(schema, list):
        values = [_gemini_compatible_schema(v) for v in schema]
        return [v for v in values if v is not None]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type" and isinstance(value, list):
            non_null = [entry for entry in value if entry != "null"]
            out[key] = non_null[0] if non_null else "string"
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _gemini_compatible_schema(item) for name, item in value.items()}
            continue
        if key == "items":
            out[key] = _gemini_compatible_schema(value)
            continue
        if isinstance(value, (dict, list)):
            out[key] = _gemini_compatible_schema(value)
        else:
            out[key] = value
    return out


def _invoice_cache_dir() -> Path:
    root = Path(__file__).resolve().parent / "reconciliation" / "invoice"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256_file(path: str | Path) -> str:
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_cache(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json_cache(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _select_invoice_vision_provider() -> Optional[str]:
    provider = (os.getenv("TELETECH_INVOICE_VISION_PROVIDER") or "").strip().lower()
    if provider in {"glm", "zai", "zhipu"}:
        return "glm"
    if provider in {"gemini", "openai"}:
        return provider
    if (os.getenv("GLM_API_KEY") or os.getenv("ZAI_API_KEY") or os.getenv("ZHIPU_API_KEY") or "").strip():
        return "glm"
    if (os.getenv("GEMINI_API_KEY") or os.getenv("TELETECH_GEMINI_API_KEY") or "").strip():
        return "gemini"
    if (os.getenv("OPENAI_API_KEY") or "").strip():
        return "openai"
    return None


def _select_invoice_vision_model(provider: str) -> str:
    if provider == "glm":
        return (os.getenv("TELETECH_INVOICE_GLM_MODEL") or "glm-4.6v").strip()
    if provider == "gemini":
        return (os.getenv("TELETECH_INVOICE_GEMINI_MODEL") or os.getenv("TELETECH_GEMINI_MODEL") or "gemini-3.8-flash").strip()
    if provider == "openai":
        return (os.getenv("TELETECH_INVOICE_OPENAI_MODEL") or "gpt-4o-mini").strip()
    raise RuntimeError("No invoice vision provider available. Set TELETECH_INVOICE_VISION_PROVIDER or an API key.")


def _invoice_api_key(provider: str) -> str:
    if provider == "glm":
        return (os.getenv("GLM_API_KEY") or os.getenv("ZAI_API_KEY") or os.getenv("ZHIPU_API_KEY") or "").strip()
    if provider == "gemini":
        return (os.getenv("GEMINI_API_KEY") or os.getenv("TELETECH_GEMINI_API_KEY") or "").strip()
    if provider == "openai":
        return (os.getenv("OPENAI_API_KEY") or "").strip()
    return ""


def _call_invoice_page_vision(page_png: str, page_no: int, provider: str, model: str, api_key: str) -> Dict[str, Any]:
    prompt = (
        INVOICE_LLM_SYSTEM_PROMPT
        + "\n\nReturn JSON only and keep the response schema exact. "
        + "Page classification values: items | certificate | other. "
        + "Certificate pages must return metadata only and zero lines."
    )

    if provider == "glm":
        b64 = base64.b64encode(Path(page_png).read_bytes()).decode("ascii")
        base_url = (os.getenv("TELETECH_INVOICE_GLM_BASE_URL") or
                    "https://api.z.ai/api/paas/v4").strip().rstrip("/")
        request_payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            "temperature": 0,
            "max_tokens": 12000,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw_response = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
            raise RuntimeError(f"GLM invoice vision HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GLM invoice vision connection failed: {exc}") from exc
        choices = raw_response.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        if isinstance(content, list):
            content = " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        text = str(content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
        if not text:
            raise RuntimeError("GLM invoice extraction returned no text output.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise RuntimeError("GLM invoice extraction did not return valid JSON.") from exc
            return json.loads(match.group(0))

    try:
        import server as server_mod  # Reuse the repo's existing Gemini/OpenAI helpers only.
    except Exception as exc:  # pragma: no cover - only if server is unavailable in isolated usage
        raise RuntimeError(f"Could not import server helpers for invoice vision: {exc}")

    if provider == "gemini":
        file_info = server_mod._gemini_upload_file(page_png, api_key, "image/png")
        contents = [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"file_data": {"file_uri": file_info["uri"], "mime_type": file_info["mime_type"]}},
            ],
        }]
        gemini_schema = _gemini_compatible_schema(INVOICE_LLM_SCHEMA)
        try:
            text, _ = server_mod._gemini_generate(model, contents, api_key, response_schema=gemini_schema)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "response_schema" not in msg and "responseschema" not in msg and "additionalproperties" not in msg:
                raise
            text, _ = server_mod._gemini_generate(model, contents, api_key)
        payload = json.loads(text)
        return payload

    if provider == "openai":
        b64 = base64.b64encode(Path(page_png).read_bytes()).decode("ascii")
        payload = {
            "model": model,
            "temperature": 0,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_file", "filename": Path(page_png).name, "file_data": f"data:image/png;base64,{b64}", "detail": "high"},
                ],
            }],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "invoice_page_extraction",
                    "strict": True,
                    "schema": INVOICE_LLM_SCHEMA,
                }
            },
            "max_output_tokens": 12000,
        }
        raw = server_mod._openai_responses_request(payload, api_key=api_key)
        text = server_mod._response_output_text(raw)
        if not text:
            raise RuntimeError("OpenAI invoice extraction returned no text output.")
        return json.loads(text)

    raise RuntimeError(f"Unsupported invoice vision provider: {provider!r}")


def _render_invoice_pages_to_png(pdf_path: str | Path, dpi: int = 220) -> List[Tuple[int, str]]:
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required for invoice LLM extraction.")
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Invoice PDF not found: {pdf_path}")
    digest = _sha256_file(pdf_path)
    cache_root = _invoice_cache_dir() / "rendered" / digest
    cache_root.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    pages: List[Tuple[int, str]] = []
    try:
        matrix = fitz.Matrix(float(dpi) / 72.0, float(dpi) / 72.0)
        for page_no in range(doc.page_count):
            png_path = cache_root / f"page_{page_no + 1}.png"
            if not png_path.exists():
                page = doc[page_no]
                pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
                pix.save(str(png_path))
            pages.append((page_no + 1, str(png_path)))
        return pages
    finally:
        doc.close()


def normalize_invoice_llm_output(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Invoice LLM output must be a JSON object.")
    document = raw.get("document") or {}
    pages = raw.get("pages") or []
    normalized = {
        "document": {
            "invoice_no": document.get("invoice_no"),
            "supply_order_no": document.get("supply_order_no"),
            "customer": document.get("customer"),
            "supplier": document.get("supplier"),
            "date": (None if str(document.get("date") or "").strip().lower() in {"", "null", "none"} else document.get("date")),
            "printed_grand_total": document.get("printed_grand_total"),
        },
        "pages": [],
    }
    for page in pages:
        if not isinstance(page, dict):
            continue
        lines = []
        for line in page.get("lines") or []:
            if not isinstance(line, dict):
                continue
            lines.append({
                "contract_item_no": line.get("contract_item_no"),
                "description_ar": line.get("description_ar"),
                "description_en": line.get("description_en"),
                "part_numbers": list(line.get("part_numbers") or []),
                "unit": line.get("unit"),
                "qty": line.get("qty"),
                "unit_price": line.get("unit_price"),
                "line_total": line.get("line_total"),
                "raw": {
                    "qty": line.get("raw", {}).get("qty"),
                    "unit_price": line.get("raw", {}).get("unit_price"),
                    "line_total": line.get("raw", {}).get("line_total"),
                },
                "confidence": line.get("confidence"),
                "flags": list(line.get("flags") or []),
            })
        normalized["pages"].append({
            "page_no": page.get("page_no"),
            "type": page.get("type"),
            "lines": lines,
        })
    return normalized


def invoice_llm_rows_for_recon(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for page in (parsed or {}).get("pages") or []:
        if not isinstance(page, dict):
            continue
        if str(page.get("type") or "").lower() != "items":
            continue
        for line in page.get("lines") or []:
            if not isinstance(line, dict):
                continue
            rows.append({
                "contract_item_no": line.get("contract_item_no"),
                "description": (line.get("description_ar") or line.get("description_en") or "").strip(),
                "description_en": line.get("description_en"),
                "part_numbers": list(line.get("part_numbers") or []),
                "unit": line.get("unit"),
                "qty": line.get("qty"),
                "unit_price": line.get("unit_price"),
                "line_total": line.get("line_total"),
                "raw": line.get("raw") or {},
                "confidence": line.get("confidence"),
                "flags": list(line.get("flags") or []),
                "page_no": page.get("page_no"),
            })
    return rows


def extract_invoice_with_vision(pdf_path: str | Path, provider: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """Render each PDF page and extract structured invoice items with the selected vision provider.

    Returns a normalized JSON document compatible with the existing reconciliation reviewer flow.
    """
    pdf_path = Path(pdf_path)
    provider = (provider or _select_invoice_vision_provider() or "").strip().lower()
    if not provider:
        raise RuntimeError("No invoice vision provider is available. Set GLM_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY.")
    if provider not in {"glm", "gemini", "openai"}:
        raise RuntimeError(f"Unsupported invoice vision provider: {provider!r}")
    key = _invoice_api_key(provider)
    if not key:
        raise RuntimeError(f"The {provider} API key is missing. Set the matching environment variable before running invoice OCR.")
    model_name = (model or _select_invoice_vision_model(provider)).strip() or _select_invoice_vision_model(provider)
    digest = _sha256_file(pdf_path)
    # Cache by provider and model as well as source PDF; changing providers must rerun extraction.
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_name)
    cache_path = _invoice_cache_dir() / f"{digest}.{provider}.{safe_model}.json"
    cached = _read_json_cache(cache_path)
    if cached:
        return normalize_invoice_llm_output(cached)

    rendered_pages = _render_invoice_pages_to_png(pdf_path)
    payload: Dict[str, Any] = {"document": {"invoice_no": None, "supply_order_no": None, "customer": None, "supplier": None, "date": None, "printed_grand_total": None}, "pages": []}
    # Per-page checkpoints allow a failed/interrupted invoice to resume without
    # sending already completed pages to the provider again.
    page_cache_dir = cache_path.parent / (cache_path.stem + ".pages")
    page_cache_dir.mkdir(parents=True, exist_ok=True)
    document_fields = ("invoice_no", "supply_order_no", "customer", "supplier", "date", "printed_grand_total")

    for page_no, page_png in rendered_pages:
        page_cache_path = page_cache_dir / f"page_{page_no:04d}.json"
        cached_page = _read_json_cache(page_cache_path)
        if cached_page and isinstance(cached_page.get("page"), dict):
            page_payload = dict(cached_page["page"])
            page_document = cached_page.get("document") or {}
        else:
            # Save each successful page immediately. If a later page hits quota,
            # the next run resumes from the first page without a checkpoint.
            raw_page = _call_invoice_page_vision(page_png, page_no, provider, model_name, key)
            page_document = raw_page.get("document") if isinstance(raw_page, dict) else {}
            if isinstance(raw_page, dict) and isinstance(raw_page.get("pages"), list):
                page_payload = raw_page["pages"][0] if raw_page["pages"] else {}
            else:
                page_payload = raw_page
            page_payload = dict(page_payload or {})
            page_payload["page_no"] = page_no
            page_payload.setdefault("type", "other")
            page_payload.setdefault("lines", [])
            page_document = page_document if isinstance(page_document, dict) else {}
            _write_json_cache(page_cache_path, {"page": page_payload, "document": page_document})

        for field in document_fields:
            value = page_document.get(field)
            if value not in (None, "", "null", "None"):
                payload["document"][field] = value
        # The renderer is authoritative for page numbering.
        page_payload["page_no"] = page_no
        page_payload.setdefault("type", "other")
        page_payload.setdefault("lines", [])
        payload["pages"].append(page_payload)

    normalized = normalize_invoice_llm_output(payload)
    _write_json_cache(cache_path, normalized)
    return normalized


__all__ = [
    "INVOICE_LLM_SCHEMA",
    "INVOICE_LLM_SYSTEM_PROMPT",
    "extract_invoice_with_vision",
    "normalize_invoice_llm_output",
    "invoice_llm_rows_for_recon",
]
