import base64
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

APP_NAME = "CIEL — Манас-интеллект"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
ACCESS_TOKEN = os.getenv("CIEL_ACCESS_TOKEN", "")
DB_PATH = os.getenv("DATABASE_PATH", "./ciel.sqlite3")

SYSTEM_PROMPT = (
    "Тебя зовут Сиэль. Ты спокойная, точная и вежливая помощница. "
    "Отвечай на языке пользователя, ясно и по делу. Не выдумывай факты, "
    "если не знаешь — скажи честно. Учитывай исправления пользователя "
    "в текущем диалоге, замечай ошибки и предлагай конкретные улучшения, "
    "когда это уместно. Не утверждай, что обучилась или изменила себя, "
    "если этого не произошло. Не изменяй файлы без явного разрешения пользователя."
)

app = FastAPI(title=APP_NAME, version="1.0.0")


def db():
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at INTEGER NOT NULL,
        messages TEXT NOT NULL
    )""")
    return con


def authorize(authorization: Optional[str]):
    if not ACCESS_TOKEN:
        raise HTTPException(503, "Backend не настроен: задайте CIEL_ACCESS_TOKEN")
    if not authorization or authorization != f"Bearer {ACCESS_TOKEN}":
        raise HTTPException(401, "Неверный токен доступа")


class ChatRequest(BaseModel):
    chat_id: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=80)
    message: str = Field(default="", max_length=12000)
    image_base64: Optional[str] = None
    image_mime: Optional[str] = "image/jpeg"


@app.get("/health")
def health():
    return {"ok": True, "service": "CIEL", "model_configured": bool(LLM_API_KEY)}


@app.get("/history")
def history(authorization: Optional[str] = Header(default=None)):
    authorize(authorization)
    with db() as con:
        rows = con.execute("SELECT id,title,updated_at FROM chats ORDER BY updated_at DESC LIMIT 100").fetchall()
    return {"chats": [dict(r) for r in rows]}


@app.get("/history/{chat_id}")
def get_chat(chat_id: str, authorization: Optional[str] = Header(default=None)):
    authorize(authorization)
    with db() as con:
        row = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Диалог не найден")
    return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"], "messages": json.loads(row["messages"])}


@app.delete("/history/{chat_id}")
def delete_chat(chat_id: str, authorization: Optional[str] = Header(default=None)):
    authorize(authorization)
    with db() as con:
        con.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    return {"ok": True}


@app.post("/chat")
async def chat(body: ChatRequest, authorization: Optional[str] = Header(default=None)):
    authorize(authorization)
    if not LLM_API_KEY:
        raise HTTPException(503, "На сервере не задан LLM_API_KEY")
    if not body.message.strip() and not body.image_base64:
        raise HTTPException(400, "Напишите сообщение или прикрепите фото")

    with db() as con:
        row = con.execute("SELECT messages,title FROM chats WHERE id=?", (body.chat_id,)).fetchone()
    messages = json.loads(row["messages"]) if row else []
    user_content = []
    if body.message.strip():
        user_content.append({"type": "text", "text": body.message.strip()})
    elif body.image_base64:
        user_content.append({"type": "text", "text": "Пожалуйста, опиши и проанализируй прикреплённое изображение."})
    if body.image_base64:
        # Limit encoded uploads to about 8 MiB to protect memory and provider costs.
        if len(body.image_base64) > 8 * 1024 * 1024 * 4 / 3:
            raise HTTPException(413, "Фото слишком большое (максимум около 8 МБ)")
        mime = body.image_mime if body.image_mime in ("image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg"
        user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{body.image_base64}"}})

    request_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    request_messages.extend(messages[-16:])
    request_messages.append({"role": "user", "content": user_content})
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json={"model": LLM_MODEL, "messages": request_messages, "temperature": 0.6, "max_tokens": 1400},
            )
        if response.status_code >= 400:
            # Avoid forwarding provider secrets or internal response details.
            raise HTTPException(502, f"Провайдер модели вернул ошибку HTTP {response.status_code}")
        data = response.json()
        answer = data["choices"][0]["message"]["content"]
        if not isinstance(answer, str):
            answer = "\n".join(part.get("text", "") for part in answer if isinstance(part, dict))
    except HTTPException:
        raise
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        raise HTTPException(502, "Не удалось получить ответ от модели. Проверьте настройки провайдера.")

    messages.extend([
        {"role": "user", "content": body.message.strip() or "[Фото]"},
        {"role": "assistant", "content": answer},
    ])
    title = row["title"] if row else (body.message.strip().replace("\n", " ")[:60] or "Диалог с фото")
    with db() as con:
        con.execute("INSERT INTO chats(id,title,updated_at,messages) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at, messages=excluded.messages",
                    (body.chat_id, title, int(time.time()), json.dumps(messages, ensure_ascii=False)))
    return {"chat_id": body.chat_id, "title": title, "answer": answer}
