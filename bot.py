#!/usr/bin/env python3
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from ytmusicapi import YTMusic

PROXY_ENV = "PROXY_URL"
PROXY_ENV_VARS = (
	"HTTP_PROXY",
	"HTTPS_PROXY",
	"ALL_PROXY",
	"http_proxy",
	"https_proxy",
	"all_proxy",
)
BOT_API_BASE = "https://api.telegram.org"
SEARCH_LIMIT = 10
CONFIDENCE_MIN = 0.60
YTM_URL = "https://music.youtube.com/watch?v={vid}"
YT_URL = "https://www.youtube.com/watch?v={vid}"
YOUTUBE_CLIENTS = ["ios", "tv_embedded", "webremix"]
POLL_TIMEOUT_S = 30
HTTP_TIMEOUT_S = 75
STARTED_AT = time.time()

PENALTY_TERMS = {
	"live",
	"remix",
	"cover",
	"sped",
	"slowed",
	"nightcore",
	"8d",
	"reverb",
	"extended",
	"mashup",
	"edit",
	"karaoke",
	"instrumental",
	"demo",
	"tribute",
	"soundalike",
}
UNKNOWN_ARTIST_VALUES = {
	"",
	"unknown artist",
	"unknown",
	"youtube",
	"music",
}
GENERIC_TITLE_SUFFIX_RE = re.compile(
	r"(?:\s*[\(\[]\s*(?:lyrics?|official(?:\s+(?:video|audio|music\s+video))?|audio|music\s+video|visualizer)\s*[\)\]]|\s*[•-]\s*(?:full\s+)?lyrics?)\s*$",
	flags=re.I,
)
CYRILLIC_TRANSLIT = {
	"А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E", "Ж": "Zh", "З": "Z",
	"И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R",
	"С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "Kh", "Ц": "Ts", "Ч": "Ch", "Ш": "Sh",
	"Щ": "Sch", "Ъ": "", "Ы": "Y", "Ь": "", "Э": "E", "Ю": "Yu", "Я": "Ya",
	"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
	"и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
	"с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
	"щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


@dataclass
class PendingChoice:
	query: str
	candidates: List[Dict]
	created_at: float


@dataclass
class PendingConfirmation:
	video_id: str
	title: str
	artist: str
	source_url: str
	requester_name: str
	requester_username: str
	requester_id: int
	created_at: float


class TelegramApiError(RuntimeError):
	pass


PENDING_CHOICES_BY_CHAT: Dict[int, PendingChoice] = {}
PENDING_CONFIRM_BY_CHAT: Dict[int, PendingConfirmation] = {}


def proxy_url() -> str:
	return os.environ.get(PROXY_ENV, "").strip()


def proxy_is_available(proxy: str) -> bool:
	try:
		parsed = urlparse(proxy)
		host = parsed.hostname
		port = parsed.port
		if not host:
			return False
		if port is None:
			port = 443 if parsed.scheme == "https" else 80
		with socket.create_connection((host, port), timeout=1):
			return True
	except OSError:
		return False


def effective_proxy_url() -> str:
	proxy = proxy_url()
	if not proxy:
		return ""
	if proxy_is_available(proxy):
		return proxy
	print(f"Proxy {proxy} is configured but unavailable; using direct connection.", file=sys.stderr)
	return ""


def configure_proxy() -> None:
	proxy = effective_proxy_url()
	if not proxy:
		for name in PROXY_ENV_VARS:
			os.environ.pop(name, None)
		return
	for name in PROXY_ENV_VARS:
		os.environ[name] = proxy


def subprocess_env() -> Dict[str, str]:
	env = os.environ.copy()
	proxy = effective_proxy_url()
	if proxy:
		for name in PROXY_ENV_VARS:
			env[name] = proxy
	else:
		for name in PROXY_ENV_VARS:
			env.pop(name, None)
	return env


def load_dotenv() -> None:
	candidates = [
		pathlib.Path(__file__).resolve().parent / ".env",
		pathlib.Path(__file__).resolve().parent.parent / ".env",
		pathlib.Path.cwd() / ".env",
	]
	for path in candidates:
		if not path.exists():
			continue
		for raw_line in path.read_text(encoding="utf-8").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, value = line.split("=", 1)
			key = key.strip()
			value = value.strip().strip("'").strip('"')
			if key and key not in os.environ:
				os.environ[key] = value
		return


def bot_token() -> str:
	token = os.environ.get("TG_BOT_TOKEN", "").strip()
	if not token:
		raise RuntimeError("Set TG_BOT_TOKEN in .env or in the environment before starting the bot.")
	return token


def wedding_group_chat_id() -> int:
	value = os.environ.get("WEDDING_GROUP_CHAT_ID", "").strip()
	if not value:
		raise RuntimeError("Set WEDDING_GROUP_CHAT_ID to the target Telegram group chat id.")
	try:
		return int(value)
	except ValueError as exc:
		raise RuntimeError("WEDDING_GROUP_CHAT_ID must be an integer chat id.") from exc


def session() -> requests.Session:
	s = requests.Session()
	s.trust_env = False
	proxy = effective_proxy_url()
	if proxy:
		s.proxies.update({"http": proxy, "https": proxy})
	retry_kwargs = {
		"total": 3,
		"connect": 3,
		"read": 2,
		"status": 3,
		"backoff_factor": 1,
		"status_forcelist": (429, 500, 502, 503, 504),
		"respect_retry_after_header": True,
	}
	try:
		retry = Retry(allowed_methods=frozenset(("GET",)), **retry_kwargs)
	except TypeError:
		retry = Retry(method_whitelist=frozenset(("GET",)), **retry_kwargs)
	adapter = HTTPAdapter(max_retries=retry)
	s.mount("http://", adapter)
	s.mount("https://", adapter)
	return s


HTTP = session()


def configure_http_session() -> None:
	global HTTP
	configure_proxy()
	HTTP = session()


def sanitize_error_message(exc: Exception) -> str:
	message = str(exc)
	token = os.environ.get("TG_BOT_TOKEN", "").strip()
	if token:
		message = message.replace(token, "<TG_BOT_TOKEN>")
	message = re.sub(r"/bot[^/\s>]+", "/bot<TG_BOT_TOKEN>", message)
	return message


def api_url(method: str) -> str:
	return f"{BOT_API_BASE}/bot{bot_token()}/{method}"


def api_call(method: str, *, data=None, files=None, timeout: int = 60) -> Dict:
	try:
		resp = HTTP.post(api_url(method), data=data, files=files, timeout=timeout)
		resp.raise_for_status()
	except requests.RequestException as exc:
		raise TelegramApiError(f"Telegram API error in {method}: {sanitize_error_message(exc)}") from exc
	payload = resp.json()
	if not payload.get("ok"):
		raise TelegramApiError(f"Telegram API error in {method}: {payload}")
	return payload["result"]


def get_updates(offset: Optional[int]) -> List[Dict]:
	try:
		resp = HTTP.get(
			api_url("getUpdates"),
			params={
				"offset": offset,
				"timeout": POLL_TIMEOUT_S,
				"allowed_updates": json.dumps(["message", "callback_query"]),
			},
			timeout=HTTP_TIMEOUT_S,
		)
		resp.raise_for_status()
	except requests.RequestException as exc:
		raise TelegramApiError(f"Telegram getUpdates failed: {sanitize_error_message(exc)}") from exc
	payload = resp.json()
	if not payload.get("ok"):
		raise TelegramApiError(f"Telegram API error in getUpdates: {payload}")
	return payload["result"]


def send_message(chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> None:
	data = {"chat_id": str(chat_id), "text": text}
	if reply_markup is not None:
		data["reply_markup"] = json.dumps(reply_markup)
	api_call("sendMessage", data=data)


def send_chat_action(chat_id: int, action: str) -> None:
	api_call("sendChatAction", data={"chat_id": str(chat_id), "action": action})


def answer_callback_query(callback_query_id: str, text: Optional[str] = None, show_alert: bool = False) -> None:
	data = {"callback_query_id": callback_query_id}
	if text:
		data["text"] = text
	if show_alert:
		data["show_alert"] = "true"
	api_call("answerCallbackQuery", data=data)


def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: Optional[Dict] = None) -> None:
	data = {"chat_id": str(chat_id), "message_id": str(message_id)}
	if reply_markup is not None:
		data["reply_markup"] = json.dumps(reply_markup)
	api_call("editMessageReplyMarkup", data=data)


def send_audio(
	chat_id: int,
	path: pathlib.Path,
	title: str,
	artist: str,
	cover_bytes: Optional[bytes],
	caption: Optional[str] = None,
) -> None:
	data = {"chat_id": str(chat_id), "title": title, "performer": artist}
	if caption:
		data["caption"] = caption[:1024]
	files = {"audio": (path.name, path.open("rb"), "audio/mp4")}
	thumb_handle = None
	try:
		if cover_bytes:
			thumb_handle = io.BytesIO(cover_bytes)
			thumb_handle.name = "cover.jpg"
			files["thumbnail"] = ("cover.jpg", thumb_handle, "image/jpeg")
		api_call("sendAudio", data=data, files=files, timeout=300)
	finally:
		files["audio"][1].close()
		if thumb_handle is not None:
			thumb_handle.close()


def toks(text: str) -> Set[str]:
	return set(re.findall(r"[^\W_]+", (text or "").lower(), flags=re.UNICODE))


def overlap_ratio(needle: Set[str], haystack: Set[str]) -> float:
	return len(needle & haystack) / max(1, len(needle))


def duration_s(value: Optional[int | str]) -> int:
	try:
		if value is None:
			return 0
		if isinstance(value, str):
			text = value.strip()
			if not text:
				return 0
			if ":" in text:
				total = 0
				for part in text.split(":"):
					total = total * 60 + int(part)
				return total
			return int(float(text))
		return int(value)
	except Exception:
		return 0


def clean_query(search_query: str) -> str:
	query = search_query.strip()
	query = re.sub(r"\s+", " ", query)
	query = query.replace(" - ", " ")
	return query


def strip_noise(search_query: str) -> str:
	text = re.sub(r"[\(\[][^\)\]]*[Ff]eat[^\)\]]*[\)\]]", " ", search_query)
	text = re.sub(r"[\(\[][Oo]fficial[^\)\]]*[\)\]]", " ", text)
	text = re.sub(r"[\(\[][Ll]ive[^\)\]]*[\)\]]", " ", text)
	text = re.sub(r"[\(\[][Rr]emix[^\)\]]*[\)\]]", " ", text)
	text = re.sub(r"[\(\)\[\]]", " ", text)
	return re.sub(r"\s+", " ", text).strip()


def query_variants(search_query: str) -> List[str]:
	base = clean_query(search_query)
	cleaned = clean_query(strip_noise(search_query))
	variants = [base]
	if cleaned and cleaned != base:
		variants.append(cleaned)
	if "&" in base:
		variants.append(base.replace("&", "and"))
	if re.search(r"\band\b", base, flags=re.I):
		variants.append(re.sub(r"\band\b", "&", base, flags=re.I))

	seen = set()
	out = []
	for item in variants:
		value = re.sub(r"\s+", " ", item).strip()
		if value and value not in seen:
			seen.add(value)
			out.append(value)
	return out


def candidate_artist_text(candidate: Dict) -> str:
	artists = candidate.get("artists")
	if artists:
		try:
			return ", ".join(artist.get("name", "") for artist in artists)
		except Exception:
			pass
	return candidate.get("author", "") or ""


def is_unknown_artist(artist: str) -> bool:
	return (artist or "").strip().lower() in UNKNOWN_ARTIST_VALUES


def clean_track_title(title: str) -> str:
	cleaned = (title or "").strip()
	while True:
		next_value = GENERIC_TITLE_SUFFIX_RE.sub("", cleaned).strip()
		if next_value == cleaned:
			return cleaned
		cleaned = next_value


def parse_artist_title(title: str) -> Optional[Tuple[str, str]]:
	parts = re.split(r"\s+[-–—]\s+", title or "", maxsplit=1)
	if len(parts) != 2:
		return None
	artist, parsed_title = (part.strip() for part in parts)
	if not artist or not parsed_title:
		return None
	return artist, clean_track_title(parsed_title)


def normalized_track_metadata(title: str, artist: str) -> Tuple[str, str]:
	title = clean_track_title(str(title or "").strip())
	artist = str(artist or "").strip()
	if is_unknown_artist(artist):
		parsed = parse_artist_title(title)
		if parsed:
			artist, title = parsed
	return title or "Unknown title", artist or "Unknown artist"


def candidate_track_metadata(candidate: Dict) -> Tuple[str, str]:
	return normalized_track_metadata(candidate.get("title") or "", candidate_artist_text(candidate))


def score_candidate(search_query: str, candidate: Dict) -> float:
	query_tokens = toks(search_query)
	candidate_title, candidate_artist = candidate_track_metadata(candidate)
	title_overlap = overlap_ratio(query_tokens, toks(candidate_title))
	artist_overlap = overlap_ratio(query_tokens, toks(candidate_artist))
	yt_duration = duration_s(candidate.get("duration_seconds"))
	duration_score = 0.7 if yt_duration <= 0 else 1.0
	channel = (candidate.get("author") or "").lower()
	channel_boost = 0.15 if ("topic" in channel or "official" in channel) else 0.0

	blob = f"{candidate_title} {candidate_artist}".lower()
	penalty = 0.0
	for term in PENALTY_TERMS:
		if term in blob:
			penalty += 0.10

	total = duration_score * 0.30 + title_overlap * 0.40 + artist_overlap * 0.25 + channel_boost - penalty
	return max(0.0, min(total, 0.99))


def search_filter(yt: YTMusic, query: str, search_filter_name: str, limit: int) -> List[Dict]:
	results = yt.search(query, filter=search_filter_name, limit=limit) or []
	candidates: List[Dict] = []
	source = "music" if search_filter_name == "songs" else "videos"
	for item in results:
		video_id = item.get("videoId")
		if not video_id:
			continue
		artists = item.get("artists")
		candidates.append({
			"videoId": video_id,
			"title": item.get("title"),
			"artists": artists if search_filter_name == "songs" else None,
			"author": (artists[0]["name"] if artists and search_filter_name == "songs" else item.get("author") or ""),
			"duration_seconds": duration_s(item.get("duration_seconds") or item.get("duration")),
			"source": source,
		})
	return candidates


def find_best(search_query: str) -> Tuple[Optional[Dict], float, List[Dict]]:
	yt = YTMusic()
	all_candidates: List[Dict] = []
	seen_ids: Set[str] = set()

	for query in query_variants(search_query):
		for search_filter_name in ("songs", "videos"):
			for candidate in search_filter(yt, query, search_filter_name, SEARCH_LIMIT):
				video_id = candidate.get("videoId")
				if not video_id or video_id in seen_ids:
					continue
				seen_ids.add(video_id)
				candidate["score"] = score_candidate(search_query, candidate)
				all_candidates.append(candidate)

	ranked = sorted(
		all_candidates,
		key=lambda item: (item["score"], 1 if item.get("source") == "music" else 0),
		reverse=True,
	)
	if not ranked:
		return None, 0.0, []

	best = ranked[0]
	if best["score"] < CONFIDENCE_MIN:
		return None, best["score"], ranked
	return best, best["score"], ranked


def sanitize_name(name: str) -> str:
	cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name or "")
	cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
	return cleaned[:120] or "file"


def transliterate_latin(text: str) -> str:
	return "".join(CYRILLIC_TRANSLIT.get(char, char) for char in text or "")


def sanitize_filename_component(name: str) -> str:
	cleaned = transliterate_latin(name)
	cleaned = re.sub(r'[\\/:*?"<>|]+', "_", cleaned)
	cleaned = re.sub(r"[^\x00-\x7F]+", "", cleaned)
	cleaned = re.sub(r"\s+", "_", cleaned).strip().strip("._")
	cleaned = re.sub(r"_+", "_", cleaned)
	return cleaned or "Unknown"


def audio_base_name(title: str, artist: str) -> str:
	artist_part = sanitize_filename_component(artist)
	title_part = sanitize_filename_component(title)
	if title_part != "Unknown":
		return sanitize_name(title_part)
	return sanitize_name(artist_part)


def is_youtube_url(text: str) -> bool:
	try:
		parsed = urlparse(text.strip())
	except Exception:
		return False
	host = (parsed.netloc or "").lower()
	return any(domain in host for domain in ("youtube.com", "youtu.be"))


def extract_youtube_video_id(url: str) -> Optional[str]:
	try:
		parsed = urlparse(url.strip())
	except Exception:
		return None

	host = (parsed.netloc or "").lower()
	path = parsed.path or ""
	if "youtu.be" in host:
		candidate = path.strip("/").split("/")[0]
		return candidate or None
	if "youtube.com" in host:
		if path == "/watch":
			query = parse_qs(parsed.query or "")
			candidate = (query.get("v") or [None])[0]
			return candidate or None
		if path.startswith("/shorts/") or path.startswith("/embed/") or path.startswith("/live/"):
			parts = [part for part in path.split("/") if part]
			if len(parts) >= 2:
				return parts[1]
	return None


def ytdlp_path() -> str:
	root = pathlib.Path(__file__).resolve().parent.parent
	local = root / "yt-dlp"
	if local.exists():
		return str(local)
	return "yt-dlp"


def add_proxy_args(cmd: List[str]) -> None:
	proxy = effective_proxy_url()
	if proxy:
		cmd.extend(["--proxy", proxy])


def probe_video_metadata(url: str, video_id: str) -> Tuple[str, str]:
	cmd = [ytdlp_path(), "--dump-single-json", "--no-playlist"]
	add_proxy_args(cmd)
	cmd.append(url)
	proc = subprocess.run(cmd, capture_output=True, text=True, env=subprocess_env())
	if proc.returncode != 0:
		return f"YouTube {video_id}", "YouTube"
	try:
		payload = json.loads(proc.stdout)
	except Exception:
		return f"YouTube {video_id}", "YouTube"
	title = (payload.get("track") or payload.get("title") or f"YouTube {video_id}").strip()
	artist = payload.get("artist") or payload.get("uploader") or payload.get("channel") or "YouTube"
	return normalized_track_metadata(str(title).strip() or f"YouTube {video_id}", str(artist).strip() or "YouTube")


def yt_thumbnail_bytes(video_id: str) -> Optional[bytes]:
	for quality in ("maxresdefault", "sddefault", "hqdefault", "mqdefault", "default"):
		url = f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"
		try:
			response = HTTP.get(url, timeout=20)
			if response.status_code == 200 and response.content and len(response.content) > 1024:
				return response.content
		except Exception:
			pass
	return None


def tag_file(path: pathlib.Path, title: str, artist: str, cover_bytes: Optional[bytes]) -> None:
	if path.suffix.lower() == ".mp3":
		try:
			_ = EasyID3(path)
		except Exception:
			try:
				EasyID3.register_text_key("date", "TDRC")
			except Exception:
				pass
			audio = EasyID3()
			audio.save(path)
		audio = EasyID3(path)
		audio["title"] = title
		audio["artist"] = artist
		audio.save()
		if cover_bytes:
			id3 = ID3(path)
			id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
			id3.save(v2_version=3)
		return

	if path.suffix.lower() in {".m4a", ".mp4"}:
		audio = MP4(path)
		audio["\xa9nam"] = [title]
		audio["\xa9ART"] = [artist]
		if cover_bytes:
			audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
		audio.save()


def download_audio(video_id: str, title: str, artist: str, out_dir: pathlib.Path) -> pathlib.Path:
	out_dir.mkdir(parents=True, exist_ok=True)
	base_name = audio_base_name(title, artist)
	out_template = str(out_dir / f"{base_name}.%(ext)s")
	last_error = "yt-dlp failed without error output"

	for base_url in (YTM_URL.format(vid=video_id), YT_URL.format(vid=video_id)):
		for client in YOUTUBE_CLIENTS:
			cmd = [
				ytdlp_path(),
				"-f",
				"ba[ext=m4a]/bestaudio[ext=m4a]/bestaudio",
				"--no-playlist",
				"--force-overwrites",
				"--retries",
				"5",
				"--fragment-retries",
				"5",
				"--socket-timeout",
				"30",
				"--extractor-args",
				f"youtube:player_client={client}",
				"-o",
				out_template,
			]
			add_proxy_args(cmd)
			cmd.append(base_url)
			proc = subprocess.run(cmd, capture_output=True, text=True, env=subprocess_env())
			if proc.returncode == 0:
				files = sorted(out_dir.glob(f"{base_name}.*"), key=lambda path: path.stat().st_mtime, reverse=True)
				if files:
					return files[0]
				raise RuntimeError("yt-dlp reported success but no file was created.")
			last_error = (proc.stderr or proc.stdout or "").strip()[-500:]
	raise RuntimeError(f"Download failed: {last_error}")


def requester_name(user: Dict) -> str:
	parts = [str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]
	full_name = " ".join(part for part in parts if part).strip()
	return full_name or str(user.get("id") or "unknown")


def requester_caption(pending: PendingConfirmation) -> str:
	username = f"@{pending.requester_username}" if pending.requester_username else f"id {pending.requester_id}"
	return (
		f"Выбор для свадьбы\n"
		f"Трек: {pending.artist} - {pending.title}\n"
		f"От: {pending.requester_name} ({username})\n"
		f"Источник: {pending.source_url}"
	)


def youtube_url(video_id: str) -> str:
	return YT_URL.format(vid=video_id)


def confirmation_buttons() -> Dict:
	return {
		"inline_keyboard": [
			[
				{"text": "Подтверждаю", "callback_data": "confirm:yes"},
				{"text": "Отмена", "callback_data": "confirm:no"},
			]
		]
	}


def format_confirmation_text(title: str, artist: str) -> str:
	return (
		f"Выбран трек:\n"
		f"{artist} - {title}\n\n"
		"Подтверждаешь выбор для свадьбы?"
	)


def store_confirmation(chat_id: int, pending: PendingConfirmation) -> None:
	PENDING_CONFIRM_BY_CHAT[chat_id] = pending
	PENDING_CHOICES_BY_CHAT.pop(chat_id, None)
	send_message(chat_id, format_confirmation_text(pending.title, pending.artist), reply_markup=confirmation_buttons())


def format_candidates(candidates: List[Dict], limit: int = 5) -> str:
	lines = ["Выбери вариант кнопкой ниже или отправь номер 1-5:"]
	for index, candidate in enumerate(candidates[:limit], start=1):
		title, artist = candidate_track_metadata(candidate)
		score = candidate.get("score", 0.0)
		source = candidate.get("source") or "unknown"
		duration = duration_s(candidate.get("duration_seconds"))
		duration_text = f"{duration}s" if duration else "unknown duration"
		lines.append(f"{index}. {artist} - {title} | {duration_text} | {source} | score={score:.2f}")
	return "\n".join(lines)


def candidate_buttons(candidates: List[Dict], limit: int = 5) -> Dict:
	keyboard = []
	for index, candidate in enumerate(candidates[:limit], start=1):
		title, artist = candidate_track_metadata(candidate)
		button_text = f"{index}. {artist[:20]} - {title[:24]}"
		keyboard.append([{"text": button_text, "callback_data": f"pick:{index}"}])
	return {"inline_keyboard": keyboard}


def cleanup_old_pending() -> None:
	now = time.time()
	for chat_id in list(PENDING_CHOICES_BY_CHAT.keys()):
		if now - PENDING_CHOICES_BY_CHAT[chat_id].created_at > 900:
			del PENDING_CHOICES_BY_CHAT[chat_id]
	for chat_id in list(PENDING_CONFIRM_BY_CHAT.keys()):
		if now - PENDING_CONFIRM_BY_CHAT[chat_id].created_at > 1800:
			del PENDING_CONFIRM_BY_CHAT[chat_id]


def handle_search(chat_id: int, user: Dict, query: str) -> None:
	send_chat_action(chat_id, "typing")
	_, confidence, ranked = find_best(query)
	if not ranked:
		send_message(chat_id, f"Не нашел варианты для: {query}")
		return
	PENDING_CHOICES_BY_CHAT[chat_id] = PendingChoice(query=query, candidates=ranked[:5], created_at=time.time())
	PENDING_CONFIRM_BY_CHAT.pop(chat_id, None)
	prefix = f"Лучшее совпадение: {confidence:.2f}\n" if confidence > 0 else ""
	send_message(chat_id, prefix + format_candidates(ranked, limit=5), reply_markup=candidate_buttons(ranked, limit=5))


def select_candidate(chat_id: int, user: Dict, index_text: str) -> None:
	pending = PENDING_CHOICES_BY_CHAT.get(chat_id)
	if not pending:
		send_message(chat_id, "Сначала отправь название трека и артиста.")
		return
	try:
		index = int(index_text.strip())
	except ValueError:
		send_message(chat_id, "Отправь номер из списка вариантов.")
		return
	if not (1 <= index <= len(pending.candidates)):
		send_message(chat_id, "Такого варианта нет в списке.")
		return

	selected = pending.candidates[index - 1]
	video_id = selected.get("videoId")
	if not video_id:
		send_message(chat_id, "У этого варианта нет YouTube video id.")
		return
	title, artist = candidate_track_metadata(selected)
	if title == "Unknown title":
		title = pending.query
	store_confirmation(
		chat_id,
		PendingConfirmation(
			video_id=video_id,
			title=title,
			artist=artist,
			source_url=youtube_url(video_id),
			requester_name=requester_name(user),
			requester_username=str(user.get("username") or "").strip(),
			requester_id=int(user.get("id") or 0),
			created_at=time.time(),
		),
	)


def handle_direct_link(chat_id: int, user: Dict, url: str) -> None:
	video_id = extract_youtube_video_id(url)
	if not video_id:
		send_message(chat_id, "Не смог достать YouTube video id из ссылки.")
		return
	send_chat_action(chat_id, "typing")
	title, artist = probe_video_metadata(url, video_id)
	store_confirmation(
		chat_id,
		PendingConfirmation(
			video_id=video_id,
			title=title,
			artist=artist,
			source_url=url,
			requester_name=requester_name(user),
			requester_username=str(user.get("username") or "").strip(),
			requester_id=int(user.get("id") or 0),
			created_at=time.time(),
		),
	)


def confirm_track(chat_id: int) -> None:
	pending = PENDING_CONFIRM_BY_CHAT.get(chat_id)
	if not pending:
		send_message(chat_id, "Выбор не найден или устарел. Отправь трек заново.")
		return
	send_message(chat_id, f"Скачиваю: {pending.artist} - {pending.title}")
	send_chat_action(chat_id, "upload_document")

	with tempfile.TemporaryDirectory(prefix="weddingmusic-") as tmpdir:
		tmp_path = pathlib.Path(tmpdir)
		output_file = download_audio(pending.video_id, pending.title, pending.artist, tmp_path)
		cover_bytes = yt_thumbnail_bytes(pending.video_id)
		tag_file(output_file, pending.title, pending.artist, cover_bytes)
		send_audio(chat_id, output_file, pending.title, pending.artist, cover_bytes)
		send_audio(
			wedding_group_chat_id(),
			output_file,
			pending.title,
			pending.artist,
			cover_bytes,
			caption=requester_caption(pending),
		)
	send_message(chat_id, "Готово, трек отправлен в свадебную группу.")
	del PENDING_CONFIRM_BY_CHAT[chat_id]


def cancel_confirmation(chat_id: int) -> None:
	PENDING_CONFIRM_BY_CHAT.pop(chat_id, None)
	send_message(chat_id, "Ок, выбор отменен.")


def build_status_text() -> str:
	try:
		group = str(wedding_group_chat_id())
	except Exception as exc:
		group = f"error: {exc}"
	return "\n".join([
		"Status:",
		f"uptime: {int(time.time() - STARTED_AT)}s",
		f"pending choices: {len(PENDING_CHOICES_BY_CHAT)}",
		f"pending confirmations: {len(PENDING_CONFIRM_BY_CHAT)}",
		f"wedding group: {group}",
		f"yt-dlp: {probe_ytdlp_status()}",
	])


def probe_ytdlp_status() -> str:
	try:
		proc = subprocess.run(
			[ytdlp_path(), "--version"],
			capture_output=True,
			text=True,
			timeout=10,
			env=subprocess_env(),
		)
	except Exception as exc:
		return f"error: {exc}"
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout or "").strip()
		return f"error: {detail or f'exit {proc.returncode}'}"
	return (proc.stdout or "").strip() or "ok"


def extract_message(update: Dict) -> Optional[Dict]:
	message = update.get("message")
	if not isinstance(message, dict):
		return None
	return message


def handle_message(message: Dict) -> None:
	user = message.get("from") or {}
	chat = message.get("chat") or {}
	chat_id = chat.get("id")
	if not isinstance(chat_id, int):
		return

	text = (message.get("text") or "").strip()
	if not text:
		return

	if text == "/start":
		send_message(
			chat_id,
			"Привет. Отправь название трека и артиста или ссылку на YouTube. "
			"После выбора я попрошу подтвердить трек для свадьбы.",
		)
		return

	if text == "/help":
		send_message(
			chat_id,
			"Как пользоваться:\n"
			"1. Отправь название трека и артиста\n"
			"2. Выбери вариант из списка\n"
			"3. Подтверди выбор для свадьбы\n"
			"4. Или сразу отправь ссылку YouTube / YouTube Music\n"
			"Бот доступен всем пользователям.",
		)
		return

	if text == "/status":
		send_chat_action(chat_id, "typing")
		send_message(chat_id, build_status_text())
		return

	if re.fullmatch(r"\d+", text):
		select_candidate(chat_id, user, text)
		return

	if is_youtube_url(text):
		handle_direct_link(chat_id, user, text)
		return

	handle_search(chat_id, user, text)


def handle_callback_query(callback_query: Dict) -> None:
	callback_query_id = callback_query.get("id")
	from_user = callback_query.get("from") or {}
	message = callback_query.get("message") or {}
	chat = message.get("chat") or {}
	chat_id = chat.get("id")
	message_id = message.get("message_id")
	data = (callback_query.get("data") or "").strip()
	if not callback_query_id or not isinstance(chat_id, int):
		return

	if data.startswith("pick:"):
		try:
			index = int(data.split(":", 1)[1])
		except Exception:
			answer_callback_query(callback_query_id, "Некорректный выбор.", show_alert=True)
			return
		answer_callback_query(callback_query_id, f"Выбран вариант {index}")
		if isinstance(message_id, int):
			try:
				edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})
			except Exception:
				pass
		select_candidate(chat_id, from_user, str(index))
		return

	if data == "confirm:yes":
		answer_callback_query(callback_query_id, "Подтверждено")
		if isinstance(message_id, int):
			try:
				edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})
			except Exception:
				pass
		confirm_track(chat_id)
		return

	if data == "confirm:no":
		answer_callback_query(callback_query_id, "Отменено")
		if isinstance(message_id, int):
			try:
				edit_message_reply_markup(chat_id, message_id, {"inline_keyboard": []})
			except Exception:
				pass
		cancel_confirmation(chat_id)
		return

	answer_callback_query(callback_query_id, "Неизвестное действие.", show_alert=False)


def run_bot() -> int:
	load_dotenv()
	configure_http_session()
	offset = None
	consecutive_errors = 0
	print("WeddingMusic bot started. Access: all Telegram users.")
	while True:
		try:
			cleanup_old_pending()
			updates = get_updates(offset)
			consecutive_errors = 0
			for update in updates:
				offset = update["update_id"] + 1
				try:
					callback_query = update.get("callback_query")
					if isinstance(callback_query, dict):
						handle_callback_query(callback_query)
						continue
					message = extract_message(update)
					if message is None:
						continue
					handle_message(message)
				except Exception as exc:
					print(
						f"Update handling error ({type(exc).__name__}, update_id={update.get('update_id')}): "
						f"{sanitize_error_message(exc)}",
						file=sys.stderr,
					)
		except KeyboardInterrupt:
			print("Bot stopped.")
			return 0
		except Exception as exc:
			consecutive_errors += 1
			delay = min(60, 3 * (2 ** min(consecutive_errors - 1, 4)))
			print(
				f"Polling error #{consecutive_errors}: {sanitize_error_message(exc)}; sleeping {delay}s",
				file=sys.stderr,
			)
			time.sleep(delay)


if __name__ == "__main__":
	raise SystemExit(run_bot())
