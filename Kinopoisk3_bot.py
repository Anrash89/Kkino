# --- ЮНИКОД-ПАТЧ (должен быть самым первым) ---
import sys, os
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# --- КОНЕЦ ПАТЧА ---

import asyncio
import logging
import re
import unicodedata
import difflib
from dataclasses import dataclass
from typing import Optional, Tuple, List, Literal, Dict, Iterable
from urllib.parse import urlparse

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

# Логи в stdout
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("kp_sspoisk_bot")

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
KINOPOISK_DEV_TOKEN = os.getenv("KINOPOISK_DEV_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")
if not KINOPOISK_DEV_TOKEN:
    raise RuntimeError("Не задан KINOPOISK_DEV_TOKEN в .env (ключ kinopoisk.dev)")

# --------- Константы ---------
HEADERS_API = {
    "User-Agent": "Mozilla/5.0 (compatible; kp-sspoisk-bot/1.3)",
    "X-API-KEY": KINOPOISK_DEV_TOKEN,
}
HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

KINOPOISK_SEARCH = "https://api.poiskkino.dev/v1.4/movie/search"
KINOPOISK_FILTER = "https://api.poiskkino.dev/v1.4/movie"
KINOPOISK_BY_ID  = "https://api.poiskkino.dev/v1.4/movie/{id}"

SSPOISK_FILM = "https://www.sspoisk.ru/film/{id}/"
SSPOISK_SERIES = "https://www.sspoisk.ru/series/{id}/"

YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|2100)\b")
Kind = Literal["film", "series"]


@dataclass
class TitleCandidate:
    kp_id: int
    name: str
    kind: Kind
    year: Optional[int]
    score: float = 0.0


# ---------- НОРМАЛИЗАЦИЯ И ПАРСИНГ ЗАПРОСА ----------
def _strip_quotes(s: str) -> str:
    s = s.strip().strip("«»\"'“”„”")
    return s

def _normalize_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.lower()
    s = _strip_quotes(s)
    s = re.sub(r"\b(фильм|кино|сериал|tv\s*series|series|movie)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parse_title_and_year(q: str) -> Tuple[str, Optional[int], Optional[Kind]]:
    q = (q or "").strip()
    kind_hint: Optional[Kind] = None
    if re.search(r"\bсериал\b", q, re.IGNORECASE):
        kind_hint = "series"
    if re.search(r"\bфильм\b", q, re.IGNORECASE):
        if kind_hint is None:
            kind_hint = "film"

    years = YEAR_RE.findall(q)
    year = int(years[-1]) if years else None

    if year:
        q = YEAR_RE.sub("", q)
    title = _normalize_title(q)
    return title, year, kind_hint


# ---------- ТИП ----------
def infer_kind(type_value: Optional[str], doc: dict) -> Kind:
    t = str(type_value or "").lower().strip()
    if t in {"movie", "video", "cartoon", "anime", "short-film"}:
        return "film"
    if any(x in t for x in ("series", "tv-series", "mini-series", "animated-series", "web-series", "tv-show")):
        return "series"
    if isinstance(doc.get("isSeries"), bool):
        return "series" if doc["isSeries"] else "film"
    if isinstance(doc.get("serial"), bool):
        return "series" if doc["serial"] else "film"
    if doc.get("seriesLength") or doc.get("seasonsInfo"):
        return "series"
    return "film"


# ---------- ОЦЕНКА ----------
def similarity(a: str, b: str) -> float:
    a, b = _normalize_title(a), _normalize_title(b)
    return difflib.SequenceMatcher(None, a, b).ratio()

def score_doc(doc: dict, user_title: str, prefer_year: Optional[int], kind_hint: Optional[Kind]) -> TitleCandidate:
    kp_id = doc.get("id") or doc.get("kpId") or doc.get("kinopoiskId")
    name = doc.get("name") or doc.get("alternativeName") or doc.get("enName") or "?"
    alt = doc.get("alternativeName") or ""
    en  = doc.get("enName") or ""
    year = doc.get("year")
    kind = infer_kind(doc.get("type"), doc)

    sim = max(
        similarity(user_title, str(name)),
        similarity(user_title, str(alt)),
        similarity(user_title, str(en)),
    )
    score = sim * 2.0
    if prefer_year is not None:
        if year == prefer_year:
            score += 2.0
        elif year and abs(year - prefer_year) == 1:
            score += 0.5
    if kind_hint and kind == kind_hint:
        score += 0.6

    return TitleCandidate(kp_id=int(kp_id), name=str(name), year=year, kind=kind, score=score)


# ---------- API ----------
async def search_exact_filter(client: httpx.AsyncClient, title: str, prefer_year: Optional[int]) -> List[TitleCandidate]:
    params = {"limit": 15}
    if title:
        params["name"] = title
    if prefer_year:
        params["year"] = prefer_year

    r = await client.get(KINOPOISK_FILTER, params=params)
    if r.status_code != 200:
        logger.warning("Filter API status %s: %s", r.status_code, r.text[:200])
        return []
    docs: List[dict] = (r.json() or {}).get("docs") or []
    cands: List[TitleCandidate] = []
    for d in docs:
        if not (d.get("id") or d.get("kinopoiskId") or d.get("kpId")):
            continue
        cands.append(score_doc(d, title, prefer_year, None))
    return cands

async def search_general(client: httpx.AsyncClient, title: str, prefer_year: Optional[int], kind_hint: Optional[Kind], limit: int = 25) -> List[TitleCandidate]:
    params = {"query": title, "limit": limit}
    r = await client.get(KINOPOISK_SEARCH, params=params)
    if r.status_code != 200:
        logger.warning("Search API status %s: %s", r.status_code, r.text[:200])
        return []
    docs: List[dict] = (r.json() or {}).get("docs") or []
    cands: List[TitleCandidate] = []
    for d in docs:
        kp_id = d.get("id") or d.get("kpId") or d.get("kinopoiskId")
        if not kp_id:
            continue
        cands.append(score_doc(d, title, prefer_year, kind_hint))
    return cands

async def search_via_kinopoisk(user_title: str, prefer_year: Optional[int], kind_hint: Optional[Kind]) -> Optional[TitleCandidate]:
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS_API) as client:
        all_cands: List[TitleCandidate] = []
        try:
            if prefer_year:
                all_cands.extend(await search_exact_filter(client, user_title, prefer_year))
        except Exception as e:
            logger.warning("Exact filter error: %s", e)
        try:
            all_cands.extend(await search_general(client, user_title, prefer_year, kind_hint))
        except Exception as e:
            logger.warning("General search error: %s", e)

    if not all_cands:
        return None

    uniq: Dict[int, TitleCandidate] = {}
    for c in all_cands:
        if c.kp_id not in uniq or c.score > uniq[c.kp_id].score:
            uniq[c.kp_id] = c
    best = sorted(uniq.values(), key=lambda x: x.score, reverse=True)[0]
    return best


# ---------- ДЕТАЛИ/СВЯЗАННЫЕ ----------
async def get_min_details(kp_id: int) -> dict:
    select = [
        "id","name","alternativeName","enName","year","type",
        "poster.url","rating.kp","genres.name",
        "sequelsAndPrequels.id","sequelsAndPrequels.name","sequelsAndPrequels.year","sequelsAndPrequels.type"
    ]
    params = {"selectFields": ",".join(select)}
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS_API) as client:
        r = await client.get(KINOPOISK_BY_ID.format(id=kp_id), params=params)
        if r.status_code != 200:
            logger.warning("ByID API status %s: %s", r.status_code, r.text[:200])
            return {}
        return r.json() or {}

def build_sspoisk_url(kp_id: int, kind: Kind) -> str:
    return (SSPOISK_SERIES if kind == "series" else SSPOISK_FILM).format(id=kp_id)

def kind_from_final_url(url: str, guessed: Kind) -> Kind:
    try:
        path = urlparse(url).path.lower()
        if "/series/" in path:
            return "series"
        if "/film/" in path:
            return "film"
    except Exception:
        pass
    return guessed

async def resolve_final_url(url: str) -> str:
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS_WEB, follow_redirects=True) as client:
        r = await client.get(url)
        return str(r.url)


# ---------- ПОМОЩНИКИ ДЛЯ СЕРИИ ----------
def _infer_kind_from_type_value(type_value: Optional[str]) -> Kind:
    t = (type_value or "").lower()
    return "series" if "series" in t else "film"

def _compact_name(name: str) -> str:
    # чтобы список выглядел опрятно в одном сообщении
    return re.sub(r"\s+", " ", (name or "?")).strip()

def select_franchise_from_details(details: dict, include_main: TitleCandidate) -> List[TitleCandidate]:
    items = []
    seqs = details.get("sequelsAndPrequels") or []
    for it in seqs:
        kp_id = it.get("id")
        if not kp_id:
            continue
        name = it.get("name") or "?"
        year = it.get("year")
        kind = _infer_kind_from_type_value(it.get("type"))
        items.append(TitleCandidate(kp_id=int(kp_id), name=_compact_name(name), year=year, kind=kind, score=0.0))

    # Добавим основной тайтл, если его нет
    present_ids = {i.kp_id for i in items}
    if include_main.kp_id not in present_ids:
        items.append(include_main)

    # Сортировка: по году, потом по имени
    items.sort(key=lambda x: (x.year or 99999, _normalize_title(x.name)))
    return items

async def fallback_franchise_search(base_query: str, prefer_year: Optional[int]) -> List[TitleCandidate]:
    """
    Если нет sequelsAndPrequels — пробуем собрать серию по расширенному поиску:
    берём результаты, где нормализованное имя содержит базовый запрос.
    """
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS_API) as client:
        cands = await search_general(client, base_query, prefer_year, None, limit=50)

    base_norm = _normalize_title(base_query)
    out: List[TitleCandidate] = []
    seen = set()
    for c in cands:
        # оставляем названия, которые содержат базовую фразу (например, "звездные войны")
        nm = _normalize_title(c.name)
        if base_norm and base_norm in nm:
            if c.kp_id not in seen:
                seen.add(c.kp_id)
                out.append(TitleCandidate(kp_id=c.kp_id, name=_compact_name(c.name), year=c.year, kind=c.kind))
    out.sort(key=lambda x: (x.year or 99999, _normalize_title(x.name)))
    return out[:30]


# ---------- ОТРИСОВКА ----------
def build_caption(details: dict, kind: Kind, link: str) -> str:
    name = details.get("name") or details.get("alternativeName") or details.get("enName") or "?"
    year = details.get("year")
    rating = (details.get("rating") or {}).get("kp")
    genres = ", ".join(g.get("name") for g in (details.get("genres") or []) if g.get("name")) or ""

    lines = [f"{'Сериал' if kind=='series' else 'Фильм'}: {name}"]
    if year:   lines.append(f"Год: {year}")
    if rating: lines.append(f"Рейтинг KP: {rating}")
    if genres: lines.append(f"Жанры: {genres}")
    lines.append(f"Смотреть: {link}")
    return "\n".join(lines)

def format_series_list(items: Iterable[TitleCandidate], max_items: int = 15) -> str:
    lines = ["Серия/франшиза:"]
    cnt = 0
    for it in items:
        link = build_sspoisk_url(it.kp_id, it.kind)
        # Строка: "• Название (год): ссылка"
        if it.year:
            line = f"• {it.name} ({it.year}): {link}"
        else:
            line = f"• {it.name}: {link}"
        lines.append(line)
        cnt += 1
        if cnt >= max_items:
            break
    if cnt == 0:
        return ""
    return "\n".join(lines)


# =========================  TELEGRAM BOT  =========================

bot = Bot(BOT_TOKEN, parse_mode=None)
dp = Dispatcher()

@dp.message(CommandStart())
async def on_start(m: Message):
    await m.answer("Пришли название — пришлю постер, год, рейтинг, жанры и ссылки на все части серии (если есть).")

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message):
    q = m.text.strip()
    try:
        raw_title, year, kind_hint = parse_title_and_year(q)
        if not raw_title:
            await m.answer("Не понял запрос. Пример: «Пила 2004», «Гарри Поттер».")
            return

        # 1) Находим основной тайтл
        best = await search_via_kinopoisk(raw_title, year, kind_hint)
        if not best:
            await m.answer("Не нашёл по API Кинопоиска. Попробуй «Название ГОД».")
            return

        # 2) Строим ссылку и получаем финальный URL (для корректного типа)
        sspoisk_url = build_sspoisk_url(best.kp_id, best.kind)
        final_url = await resolve_final_url(sspoisk_url)
        best.kind = kind_from_final_url(final_url, best.kind)

        # 3) Детали + sequels/prequels
        details = await get_min_details(best.kp_id)
        poster = ((details.get("poster") or {}).get("url")) or None

        # Сбор серии
        series_items = select_franchise_from_details(details, include_main=best)
        if len(series_items) <= 1:
            # Фоллбек: расширенный поиск по исходному названию (без года)
            series_items = await fallback_franchise_search(raw_title, year)
            # если по фоллбеку тоже пусто — оставим только основной
            if not series_items:
                series_items = [best]

        # 4) Шлём постер + краткую инфу
        caption = build_caption(details, best.kind, final_url)
        if poster:
            try:
                await m.answer_photo(photo=poster, caption=caption)
            except Exception:
                await m.answer(caption)
        else:
            await m.answer(caption)

        # 5) Отдельным сообщением шлём список всех частей с ссылками
        series_text = format_series_list(series_items, max_items=15)
        if series_text:
            await m.answer(series_text)

    except Exception as e:
        logging.exception("Ошибка обработки запроса: %s", e)
        await m.answer(f"Ошибка: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


