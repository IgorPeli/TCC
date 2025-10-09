import os
import uuid
from pathlib import Path
from datetime import datetime, timezone
from contextlib import closing

from flask import Flask, request, redirect, url_for, render_template_string, Response, abort
from werkzeug.utils import secure_filename
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pymysql

# ------------------ ENV vindas do CloudFormation ------------------
DATABASE_ENDPOINT = os.environ.get("database_endpoint") or os.environ.get("DB_HOST", "")
DB_NAME = os.environ.get("DB_NAME", "appdb")
DB_USER = os.environ.get("DB_USER", "appuser")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "admin")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = (os.environ.get("S3_PREFIX", "uploads") or "uploads").strip("/")

# ------------------ Cliente S3 (SigV4 + virtual-hosted + HTTPS) ------------------
# Tráfego do container para o S3 sairá pelo VPC Endpoint (Gateway) ligado à PrivateRT.
s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    use_ssl=True,
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

# ------------------ Flask ------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB por upload

# ------------------ Conexão com MySQL (PyMySQL) ------------------
def get_db_connection():
    try:
        conn = pymysql.connect(
            host=DATABASE_ENDPOINT,
            user=DB_USER,
            password=DB_PASSWORD,
            port=3306,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )
        return conn
    except Exception as e:
        print(f"[db] Erro de conexão: {e}")
        raise

# ------------------ Bootstrap do schema (tolerante) ------------------
DDL = """
CREATE TABLE IF NOT EXISTS posts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  text_content TEXT NOT NULL,
  s3_key VARCHAR(512) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def ensure_schema():
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor() as cur:
                cur.execute(f"USE `{DB_NAME}`;")
                cur.execute(DDL)
        print("[schema] Verificado/criado com sucesso.")
    except Exception as e:
        print(f"[schema] Ignorando erro inicial: {e}")

ensure_schema()

# ------------------ Template ------------------
PAGE = """
<!doctype html>
<html lang="pt-br">
  <head>
    <meta charset="utf-8"/>
    <title>Flask + S3 + RDS</title>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 2rem; }
      form { display: grid; gap: .75rem; max-width: 520px; margin-bottom: 2rem; }
      textarea, input[type="file"] { padding: .6rem; }
      button { padding: .6rem .9rem; cursor: pointer; }
      .card { border: 1px solid #ddd; border-radius: 10px; padding: 1rem; margin: .5rem 0; }
      img { max-width: 360px; height: auto; display:block; margin-top:.5rem; border-radius: 8px; }
      .grid { display:grid; gap:.75rem; grid-template-columns: repeat(auto-fill, minmax(360px,1fr)); }
      small { color: #555; }
    </style>
  </head>
  <body>
    <h1>Enviar texto + imagem</h1>
    <form action="{{ url_for('submit') }}" method="post" enctype="multipart/form-data">
      <label>Texto:</label>
      <textarea name="text_content" rows="3" required placeholder="Digite algo..."></textarea>

      <label>Imagem (png/jpg/jpeg):</label>
      <input type="file" name="photo" accept=".png,.jpg,.jpeg" required />

      <button type="submit">Salvar</button>
    </form>

    <h2>Publicações</h2>
    <div class="grid">
      {% for p in posts %}
      <div class="card">
        <div><strong>ID:</strong> {{ p.id }} &nbsp; <small>{{ p.created_at }}</small></div>
        <div><strong>Texto:</strong> {{ p.text_content }}</div>
        {% if p.has_image %}
          <img src="{{ url_for('image', post_id=p.id) }}" alt="imagem"/>
        {% else %}
          <small>(sem imagem)</small>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </body>
</html>
"""

# ------------------ Rotas ------------------
@app.get("/")
def index():
    rows = []
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor() as cur:
                cur.execute(f"USE `{DB_NAME}`;")
                cur.execute("SELECT id, text_content, s3_key, created_at FROM posts ORDER BY id DESC LIMIT 50;")
                rows = cur.fetchall()
    except Exception as e:
        print(f"[index] erro ao consultar posts: {e}")

    posts = []
    for r in rows or []:
        posts.append({
            "id": r.get("id"),
            "text_content": r.get("text_content"),
            "created_at": r.get("created_at"),
            "has_image": bool(r.get("s3_key")),
        })

    return render_template_string(PAGE, posts=posts)

@app.post("/submit")
def submit():
    text_content = request.form.get("text_content", "").strip()
    file = request.files.get("photo")

    if not text_content or not file:
        return redirect(url_for("index"))

    # Nome seguro + extensão; chave final = UUID + extensão (independe do nome do PC)
    original = secure_filename(file.filename or "")
    ext = Path(original).suffix.lower() or ".bin"
    key = f"{S3_PREFIX}/{uuid.uuid4().hex}{ext}"

    # Upload para S3 (via VPCE)
    try:
        s3.upload_fileobj(
            Fileobj=file,
            Bucket=S3_BUCKET,
            Key=key,
            ExtraArgs={"ContentType": file.mimetype or "application/octet-stream"}
        )
    except Exception as e:
        print(f"[submit] erro no upload S3: {e}")
        return redirect(url_for("index"))

    # Insert no RDS
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor() as cur:
                cur.execute(f"USE `{DB_NAME}`;")
                cur.execute(
                    "INSERT INTO posts (text_content, s3_key, created_at) VALUES (%s, %s, %s);",
                    (text_content, key, datetime.now(timezone.utc)),
                )
    except Exception as e:
        print(f"[submit] erro ao inserir no DB: {e}")

    return redirect(url_for("index"))

@app.get("/image/<int:post_id>")
def image(post_id: int):
    """
    Serve a imagem via Flask (S3 -> ECS -> ALB), garantindo tráfego pelo VPCE.
    """
    if not S3_BUCKET:
        abort(404)

    key = None
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor() as cur:
                cur.execute(f"USE `{DB_NAME}`;")
                cur.execute("SELECT s3_key FROM posts WHERE id=%s;", (post_id,))
                row = cur.fetchone()
                if row:
                    key = row["s3_key"]
    except Exception as e:
        print(f"[image] erro DB: {e}")

    if not key:
        abort(404)

    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        content_type = obj.get("ContentType", "application/octet-stream")
        content_length = obj.get("ContentLength")

        body = obj["Body"]  # StreamingBody

        def generate():
            try:
                for chunk in iter(lambda: body.read(64 * 1024), b""):
                    yield chunk
            finally:
                body.close()

        headers = {"Content-Type": content_type}
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        # cache leve (opcional)
        headers.setdefault("Cache-Control", "private, max-age=300")

        return Response(generate(), headers=headers)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            abort(404)
        print(f"[image] erro S3 ({code}): {e}")
        abort(500)
    except Exception as e:
        print(f"[image] erro S3: {e}")
        abort(500)

@app.get("/healthcheck")
def healthcheck():
    # Healthcheck leve: não depende do DB.
    return ("ok", 200)

@app.get("/dbcheck")
def dbcheck():
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1;")
        return ("db ok", 200)
    except Exception as e:
        return (f"db erro: {e}", 500)

if __name__ == "__main__":
    # Execução local
    app.run(host="0.0.0.0", port=8080, debug=True)
