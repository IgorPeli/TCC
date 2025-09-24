import os
import uuid
from datetime import datetime, timezone
from urllib.parse import quote_plus
from contextlib import closing

from flask import Flask, request, redirect, url_for, render_template_string
from werkzeug.utils import secure_filename
import boto3
import pymysql


# ------------------ ENV vindas do CloudFormation ------------------
# Você disse que vai colocar assim no template:
# database_endpoint = !Ref {endpoint}
# Então eu leio exatamente essa env; mantenho um fallback para DB_HOST por garantia.
DATABASE_ENDPOINT = os.environ.get("database_endpoint") or os.environ.get("DB_HOST", "")
DB_NAME = os.environ.get("DB_NAME", "appdb")
DB_USER = os.environ.get("DB_USER", "appuser")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "admin")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "uploads")

# ------------------ Cliente S3 ------------------
s3 = boto3.client("s3", region_name=AWS_REGION)

# ------------------ Flask ------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB por upload


# ------------------ Conexão com PyMySQL: do jeitinho que você pediu ------------------
def get_db_connection():
    """
    Abre uma conexão nova com o MySQL/Aurora usando PyMySQL.
    Cada chamada retorna uma conexão diferente (seguro para threads/processos).
    """
    try:
        conn = pymysql.connect(
            host=DATABASE_ENDPOINT,
            user=DB_USER,
            password=DB_PASSWORD,
            port=3306,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,  # rows como dicts
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )
        return conn
    except Exception as e:
        print(f"Erro de conexão: {e}")
        raise


# ------------------ Bootstrap do schema (se precisar) ------------------
DDL = """
CREATE TABLE IF NOT EXISTS posts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  text_content TEXT NOT NULL,
  s3_key VARCHAR(512) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def ensure_schema():
    # Garante que estamos no DB certo e que a tabela exista
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            # Se o DB já existir (CloudFormation costuma criar), isso só faz o "USE".
            # Se QUISER criar o DB aqui, descomente a linha abaixo:
            # cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` DEFAULT CHARACTER SET utf8mb4")
            cur.execute(f"USE `{DB_NAME}`;")
            cur.execute(DDL)

ensure_schema()


# ------------------ Template simples ------------------
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
      input[type="text"], textarea, input[type="file"] { padding: .6rem; }
      button { padding: .6rem .9rem; cursor: pointer; }
      .card { border: 1px solid #ddd; border-radius: 10px; padding: 1rem; margin: .5rem 0; }
      img { max-width: 360px; height: auto; display:block; margin-top:.5rem; border-radius: 8px; }
      .grid { display:grid; gap:.75rem; grid-template-columns: repeat(auto-fill, minmax(360px,1fr)); }
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
        {% if p.url %}
          <img src="{{ p.url }}" alt="imagem"/>
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
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"USE `{DB_NAME}`;")
            cur.execute("SELECT id, text_content, s3_key, created_at FROM posts ORDER BY id DESC LIMIT 50;")
            rows = cur.fetchall()

    posts = []
    for r in rows:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": r["s3_key"]},
            ExpiresIn=3600,
        )
        posts.append({
            "id": r["id"],
            "text_content": r["text_content"],
            "created_at": r["created_at"],
            "url": url
        })

    return render_template_string(PAGE, posts=posts)


@app.post("/submit")
def submit():
    text_content = request.form.get("text_content", "").strip()
    file = request.files.get("photo")

    if not text_content or not file:
        return redirect(url_for("index"))

    filename = secure_filename(file.filename or "")
    if not filename:
        return redirect(url_for("index"))

    key = f"{S3_PREFIX}/{uuid.uuid4().hex}_{filename}"

    # Upload direto para S3
    s3.upload_fileobj(
        Fileobj=file,
        Bucket=S3_BUCKET,
        Key=key,
        ExtraArgs={"ContentType": file.mimetype or "application/octet-stream"}
    )

    # Insert no RDS
    with closing(get_db_connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(f"USE `{DB_NAME}`;")
            cur.execute(
                "INSERT INTO posts (text_content, s3_key, created_at) VALUES (%s, %s, %s);",
                (text_content, key, datetime.now(timezone.utc)),
            )
    return redirect(url_for("index"))


@app.get("/healthcheck")
def healthcheck():
    try:
        with closing(get_db_connection()) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1;")
        return("ok", 200)
    except Exception as e:
        return(f"Erro: {e}", 500)

   
if __name__ == "__main__":
    app.r
