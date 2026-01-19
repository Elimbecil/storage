import os
import io
import json
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

import cloudinary
import cloudinary.uploader


# =========================
# CONFIG
# =========================
APP_TITLE = "File Vault (Cloudinary, sin BD)"
BASE_DIR = Path(__file__).parent
STORAGE_DIR = BASE_DIR / "storage"          # cache local (no confiable en Streamlit Cloud)
INDEX_PATH = STORAGE_DIR / "index.json"     # cache local del índice (la fuente real será Cloudinary)

# Dónde guardamos el índice en Cloudinary (raw)
VAULT_INDEX_PUBLIC_ID = os.environ.get("VAULT_INDEX_PUBLIC_ID", "filevault/index").strip()
VAULT_INDEX_RESOURCE_TYPE = "raw"  # index.json como raw

st.set_page_config(page_title=APP_TITLE, page_icon="🗄️", layout="wide")


# =========================
# SECRETS / AUTH
# =========================
def get_secret(key: str, default: str = "") -> str:
    # Streamlit Cloud -> st.secrets; local -> env
    return (os.environ.get(key) or st.secrets.get(key, default) or default).strip()


VAULT_PASSWORD = get_secret("VAULT_PASSWORD", "")
CLOUDINARY_URL = get_secret("CLOUDINARY_URL", "")

if not CLOUDINARY_URL:
    st.error("Falta CLOUDINARY_URL en Secrets/entorno. Añádelo para guardar en Cloudinary.")
    st.stop()

# Cloudinary toma credenciales desde CLOUDINARY_URL
os.environ["CLOUDINARY_URL"] = CLOUDINARY_URL

# Password gate
if VAULT_PASSWORD:
    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False

    if not st.session_state.auth_ok:
        st.title("🔒 Acceso protegido")
        pwd = st.text_input("Contraseña", type="password")
        if st.button("Entrar", type="primary"):
            if pwd == VAULT_PASSWORD:
                st.session_state.auth_ok = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta")
        st.stop()


# =========================
# UTILS
# =========================
def ensure_dirs():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(name: str) -> str:
    name = (name or "").strip().replace(" ", "_")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    return "".join(ch if ch in allowed else "_" for ch in name)


def human_size(num_bytes: int) -> str:
    n = float(num_bytes or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{int(num_bytes)} B"


def add_file_record(index: dict, record: dict):
    index.setdefault("files", [])
    index["files"].insert(0, record)


def delete_file_record(index: dict, file_id: str):
    index["files"] = [f for f in index.get("files", []) if f.get("id") != file_id]


def matches(rec: dict, query: str) -> bool:
    if not query:
        return True
    query = query.lower().strip()
    name = rec.get("original_name", "").lower()
    tags = " ".join(rec.get("tags", [])).lower()
    scope = rec.get("scope", "general").lower()
    return query in name or query in tags or query in scope


def upload_index_to_cloudinary(index_data: dict) -> None:
    """
    Guarda index.json como asset RAW en Cloudinary, sobrescribiendo el anterior.
    """
    payload = json.dumps(index_data, ensure_ascii=False, indent=2).encode("utf-8")

    cloudinary.uploader.upload(
        payload,
        public_id=VAULT_INDEX_PUBLIC_ID,   # fijo
        resource_type=VAULT_INDEX_RESOURCE_TYPE,
        overwrite=True,
        folder=None,                        # public_id ya incluye ruta
    )

    # cache local (útil para debug, pero no es la fuente de verdad en Cloud)
    INDEX_PATH.write_text(payload.decode("utf-8"), encoding="utf-8")


def download_index_from_cloudinary() -> dict:
    """
    Intenta descargar el index desde Cloudinary. Si no existe todavía, crea uno vacío.
    """
    try:
        # Genera URL del raw asset y lo pide por HTTP
        # Nota: el raw asset tiene URL pública (secure_url) una vez creado.
        # Si aún no existe, esta llamada recognize fallback.
        res = cloudinary.api.resource(
            VAULT_INDEX_PUBLIC_ID,
            resource_type=VAULT_INDEX_RESOURCE_TYPE
        )
        url = res.get("secure_url")
        if not url:
            raise RuntimeError("Index sin secure_url")

        r = requests.get(url, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        empty = {"files": []}
        try:
            upload_index_to_cloudinary(empty)
        except Exception:
            # si falla la creación, devolvemos vacío igualmente
            pass
        return empty


def cloudinary_upload_file(data: bytes, scope: str, original_name: str, now: datetime) -> dict:
    """
    Sube cualquier archivo a Cloudinary usando resource_type='auto'
    """
    folder = f"filevault/{scope}/{now.strftime('%Y-%m')}"
    upload_res = cloudinary.uploader.upload(
        data,
        folder=folder,
        resource_type="auto",   # permite imágenes + pdf + zip + etc  [oai_citation:1‡Cloudinary](https://cloudinary.com/documentation/upload_parameters?utm_source=chatgpt.com)
        use_filename=True,
        unique_filename=True,
    )
    return upload_res


def cloudinary_delete_asset(public_id: str, resource_type: str) -> None:
    """
    Borra asset en Cloudinary (image/video/raw).
    """
    cloudinary.uploader.destroy(public_id, resource_type=resource_type, invalidate=True)  #  [oai_citation:2‡Cloudinary](https://cloudinary.com/documentation/delete_assets?utm_source=chatgpt.com)


# =========================
# INIT
# =========================
ensure_dirs()
idx = download_index_from_cloudinary()
files = idx.get("files", [])


# =========================
# SIDEBAR
# =========================
st.sidebar.title("⚙️ Herramientas")

if st.sidebar.button("🔄 Recargar índice"):
    idx = download_index_from_cloudinary()
    files = idx.get("files", [])
    st.rerun()

if st.sidebar.button("📦 Backup (index + manifest)"):
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        index_txt = json.dumps(idx, ensure_ascii=False, indent=2)
        z.writestr("index.json", index_txt)

        # manifest con URLs
        lines = []
        for f in idx.get("files", []):
            ci = f.get("cloudinary", {})
            lines.append(
                f'{f.get("uploaded_at","")} | {f.get("scope","general")} | {f.get("original_name","")} | {ci.get("resource_type","")} | {ci.get("secure_url","")}'
            )
        z.writestr("manifest.txt", "\n".join(lines))

    mem.seek(0)
    st.sidebar.download_button("⬇️ Descargar ZIP", mem, file_name="vault_backup.zip")

st.sidebar.divider()
st.sidebar.write(f"📄 Archivos en índice: **{len(files)}**")
st.sidebar.caption(f"Índice Cloudinary: public_id = {VAULT_INDEX_PUBLIC_ID} (raw)")


# =========================
# UI
# =========================
st.title("🗄️ File Vault (Cloudinary, sin BD)")
st.caption("Los archivos se guardan en Cloudinary y el índice también (para que no se pierda al reiniciar Streamlit Cloud).")

col1, col2 = st.columns([1, 1], gap="large")

# ---------- SUBIR ----------
with col1:
    st.subheader("⬆️ Subir archivo")

    scope = st.text_input("Carpeta / Proyecto", value="general", help="Ej: cliente_a, proyecto_x").strip().lower()
    scope = safe_filename(scope) or "general"

    up = st.file_uploader("Elige un archivo", type=None)
    tags_txt = st.text_input("Tags (separados por coma)", placeholder="facturas, cliente_x, enero")

    if up is not None and st.button("Guardar en vault", type="primary"):
        now = datetime.now()

        original_name = up.name
        file_id = uuid.uuid4().hex
        data = up.getbuffer().tobytes()

        try:
            upload_res = cloudinary_upload_file(data, scope, original_name, now)
            record = {
                "id": file_id,
                "scope": scope,
                "original_name": original_name,
                "uploaded_at": now.isoformat(timespec="seconds"),
                "tags": [t.strip() for t in tags_txt.split(",") if t.strip()],
                "cloudinary": {
                    "public_id": upload_res.get("public_id"),
                    "secure_url": upload_res.get("secure_url"),
                    "bytes": int(upload_res.get("bytes", len(data))),
                    "resource_type": upload_res.get("resource_type"),  # image / video / raw
                    "format": upload_res.get("format"),
                },
            }

            add_file_record(idx, record)
            upload_index_to_cloudinary(idx)

            st.success(f"Guardado en Cloudinary: {original_name}")
            st.rerun()

        except Exception as e:
            st.error(f"Error subiendo a Cloudinary: {e}")

# ---------- LISTAR ----------
with col2:
    st.subheader("📂 Archivos")

    all_scopes = sorted(list({f.get("scope", "general") for f in files})) if files else []
    scope_filter = st.selectbox("Ver carpeta", ["(todas)"] + all_scopes, index=0)

    q = st.text_input("Buscar por nombre o tag", placeholder="factura, png, cliente_x...")

    filtered = []
    for f in files:
        if scope_filter != "(todas)" and f.get("scope", "general") != scope_filter:
            continue
        if matches(f, q):
            filtered.append(f)

    st.write(f"Mostrando **{len(filtered)}** de **{len(files)}**")

    for rec in filtered:
        ci = rec.get("cloudinary", {})
        url = ci.get("secure_url", "")
        public_id = ci.get("public_id", "")
        rtype = ci.get("resource_type", "image")
        size_bytes = int(ci.get("bytes", 0))

        with st.container(border=True):
            left, right = st.columns([3, 2], gap="large")

            with left:
                st.markdown(f"**{rec.get('original_name','(sin nombre)')}**")
                st.caption(
                    f"Carpeta: {rec.get('scope','general')} · "
                    f"Subido: {rec.get('uploaded_at','')} · "
                    f"Tamaño: {human_size(size_bytes)} · "
                    f"Tags: {', '.join(rec.get('tags', [])) or '-'}"
                )

                # Preview simple: si es imagen, mostramos directamente
                # (Cloudinary devuelve resource_type; para imágenes suele ser "image")
                if rtype == "image" and url:
                    st.image(url)

                # Para textos/JSON pequeños, intentamos mostrar (si es raw y accesible)
                ext = Path(rec.get("original_name", "")).suffix.lower()
                if ext in [".txt", ".csv", ".log", ".md", ".json"] and url:
                    try:
                        rr = requests.get(url, timeout=15)
                        if rr.ok:
                            st.code(rr.text[:2500])
                    except Exception:
                        pass

            with right:
                if url:
                    st.link_button("⬇️ Descargar / Abrir", url, use_container_width=True)
                else:
                    st.warning("Sin URL")

                if st.button("🗑️ Borrar", use_container_width=True, key=f"del_{rec.get('id')}"):
                    try:
                        if public_id:
                            cloudinary_delete_asset(public_id, rtype)
                        delete_file_record(idx, rec.get("id"))
                        upload_index_to_cloudinary(idx)
                        st.rerun()
                    except Exception as e:
                        st.error(f"No se pudo borrar: {e}")