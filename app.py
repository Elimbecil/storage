import os
import io
import json
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import streamlit as st

# =========================
# CONFIG
# =========================
APP_TITLE = "File Vault (súper simple, sin base de datos)"
BASE_DIR = Path(__file__).parent
STORAGE_DIR = BASE_DIR / "storage"
FILES_DIR = STORAGE_DIR / "files"
INDEX_PATH = STORAGE_DIR / "index.json"

st.set_page_config(page_title=APP_TITLE, page_icon="🗄️", layout="wide")


# =========================
# AUTH SIMPLE (OPCIONAL)
# =========================
# Si defines VAULT_PASSWORD en el entorno, pedirá contraseña.
# Ejemplo:
#   export VAULT_PASSWORD="tu_clave"
#   streamlit run app.py
VAULT_PASSWORD = os.environ.get("VAULT_PASSWORD", "").strip()
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
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_PATH.exists():
        INDEX_PATH.write_text(
            json.dumps({"files": []}, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_index() -> dict:
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"files": []}


def save_index(data: dict) -> None:
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_PATH)


def safe_filename(name: str) -> str:
    name = (name or "").strip().replace(" ", "_")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    return "".join(ch if ch in allowed else "_" for ch in name)


def list_scopes() -> list[str]:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    scopes = ["general"]
    for p in FILES_DIR.iterdir():
        if p.is_dir():
            scopes.append(p.name)
    # dedupe manteniendo orden
    seen = set()
    out = []
    for s in scopes:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


def scope_root(scope: str) -> Path:
    scope = safe_filename((scope or "general").strip().lower()) or "general"
    root = FILES_DIR / scope
    root.mkdir(parents=True, exist_ok=True)
    return root


def month_folder_for_scope(scope: str, dt: datetime) -> Path:
    root = scope_root(scope)
    folder = root / dt.strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def add_file_record(index: dict, record: dict):
    index.setdefault("files", [])
    index["files"].insert(0, record)  # lo último arriba


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


def human_size(num_bytes: int) -> str:
    # simple y útil
    n = float(num_bytes or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{int(num_bytes)} B"


# =========================
# INIT
# =========================
ensure_dirs()
idx = load_index()
files = idx.get("files", [])


# =========================
# SIDEBAR TOOLS
# =========================
st.sidebar.title("⚙️ Herramientas")
st.sidebar.caption("Sin base de datos: solo disco + index.json")

if st.sidebar.button("📦 Generar backup .zip"):
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        # index
        if INDEX_PATH.exists():
            z.write(INDEX_PATH, arcname="index.json")
        # archivos
        base_files = STORAGE_DIR / "files"
        if base_files.exists():
            for root, _, filenames in os.walk(base_files):
                for fn in filenames:
                    full = Path(root) / fn
                    arc = str(full.relative_to(STORAGE_DIR))
                    z.write(full, arcname=arc)
    mem.seek(0)
    st.sidebar.download_button("⬇️ Descargar backup", mem, file_name="vault_backup.zip")

st.sidebar.divider()
st.sidebar.write(f"📁 Scopes detectados: **{len(list_scopes())}**")
st.sidebar.write(f"📄 Archivos en índice: **{len(files)}**")


# =========================
# UI
# =========================
st.title("🗄️ File Vault (súper simple, sin base de datos)")
st.caption("Guarda archivos en disco + un index.json para listarlos y buscarlos. Incluye carpetas por cliente/proyecto.")

col1, col2 = st.columns([1, 1], gap="large")

# ---------- SUBIR ----------
with col1:
    st.subheader("⬆️ Subir archivo")

    scopes = list_scopes()
    scope_selected = st.selectbox("Carpeta / Proyecto", scopes, index=0)

    new_scope = st.text_input("Crear nueva carpeta (opcional)", placeholder="cliente_nuevo o proyecto_x")
    scope = safe_filename(new_scope.strip().lower()) if new_scope.strip() else scope_selected

    up = st.file_uploader("Elige un archivo", type=None)
    tags_txt = st.text_input("Tags (separados por coma)", placeholder="facturas, cliente_x, enero")

    if up is not None:
        if st.button("Guardar en vault", type="primary"):
            now = datetime.now()
            folder = month_folder_for_scope(scope, now)

            original_name = up.name
            clean_name = safe_filename(original_name)
            file_id = uuid.uuid4().hex
            stored_name = f"{file_id}__{clean_name}"
            stored_path = folder / stored_name

            data = up.getbuffer()
            with open(stored_path, "wb") as f:
                f.write(data)

            record = {
                "id": file_id,
                "scope": scope,
                "original_name": original_name,
                "stored_relpath": str(stored_path.relative_to(STORAGE_DIR)),
                "size_bytes": int(len(data)),
                "uploaded_at": now.isoformat(timespec="seconds"),
                "tags": [t.strip() for t in tags_txt.split(",") if t.strip()],
            }

            add_file_record(idx, record)
            save_index(idx)
            st.success(f"Guardado en '{scope}': {original_name}")
            st.rerun()

# ---------- LISTAR ----------
with col2:
    st.subheader("📂 Archivos")

    all_scopes = sorted(list({f.get("scope", "general") for f in files}))
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
        stored_path = STORAGE_DIR / rec["stored_relpath"]
        if not stored_path.exists():
            with st.container(border=True):
                st.warning(f"Archivo faltante en disco: {rec.get('original_name')} (id: {rec.get('id')})")
            continue

        with st.container(border=True):
            left, right = st.columns([3, 2], gap="large")

            with left:
                st.markdown(f"**{rec['original_name']}**")
                st.caption(
                    f"Carpeta: {rec.get('scope','general')} · "
                    f"Subido: {rec.get('uploaded_at')} · "
                    f"Tamaño: {human_size(rec.get('size_bytes', 0))} · "
                    f"Tags: {', '.join(rec.get('tags', [])) or '-'}"
                )

                # Preview simple
                ext = Path(rec["original_name"]).suffix.lower()

                if ext in [".png", ".jpg", ".jpeg", ".webp"]:
                    st.image(str(stored_path))
                elif ext in [".txt", ".csv", ".log", ".md", ".json"]:
                    try:
                        content = stored_path.read_text(encoding="utf-8", errors="ignore")
                        st.code(content[:2500])
                    except Exception:
                        pass

                st.caption(f"Ruta interna: {rec.get('stored_relpath')}")

            with right:
                # Descargar
                with open(stored_path, "rb") as f:
                    st.download_button(
                        label="⬇️ Descargar",
                        data=f,
                        file_name=rec["original_name"],
                        mime="application/octet-stream",
                        use_container_width=True,
                        key=f"dl_{rec['id']}",
                    )

                # Borrar
                if st.button("🗑️ Borrar", use_container_width=True, key=f"del_{rec['id']}"):
                    try:
                        stored_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    delete_file_record(idx, rec["id"])
                    save_index(idx)
                    st.rerun()
                    