"""
VoiceVox / SHAREVOX / VoiceVox NEMO HTTP API クライアント
Resolve_Script (nakano000) の設計を参考に改良
  - マルチエンジン対応
  - リトライロジック
  - 型付きデータクラス (Speaker / Mora / AccentPhrase / AudioQuery)
  - OpenJTalk ベースの mora → 口形状マッピング
"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

# ── エンジン定義 ─────────────────────────────────────────────────────────────

ENGINES: dict[str, int] = {
    "VoiceVox":       50021,
    "SHAREVOX":       50025,
    "VoiceVox NEMO":  50121,
}
DEFAULT_ENGINE = "VoiceVox"
DEFAULT_HOST   = "127.0.0.1"

# ── 口形状キー ───────────────────────────────────────────────────────────────

SHAPE_KEYS  = ["a", "i", "u", "e", "o", "N", "closed"]
SHAPE_LABEL = {"a":"あ","i":"い","u":"う","e":"え","o":"お","N":"ん","closed":"閉じ"}

# 母音文字 → 口形状
_VOWEL_TO_SHAPE: dict[str, str] = {
    "a":"a", "i":"i", "u":"u", "e":"e", "o":"o",
    "N":"N",          # 撥音 ん
    "cl":"closed",    # 促音 っ
    "pau":"closed",   # 無音ポーズ
}

# OpenJTalk mora → (子音, 母音) の完全マッピング (Resolve_Script/mora_list.py 参照)
_MORA_LIST: list[tuple[str, str, str]] = [
    # 五十音
    ("ア","","a"),("イ","","i"),("ウ","","u"),("エ","","e"),("オ","","o"),
    ("カ","k","a"),("キ","k","i"),("ク","k","u"),("ケ","k","e"),("コ","k","o"),
    ("サ","s","a"),("シ","sh","i"),("ス","s","u"),("セ","s","e"),("ソ","s","o"),
    ("タ","t","a"),("チ","ch","i"),("ツ","ts","u"),("テ","t","e"),("ト","t","o"),
    ("ナ","n","a"),("ニ","n","i"),("ヌ","n","u"),("ネ","n","e"),("ノ","n","o"),
    ("ハ","h","a"),("ヒ","h","i"),("フ","f","u"),("ヘ","h","e"),("ホ","h","o"),
    ("マ","m","a"),("ミ","m","i"),("ム","m","u"),("メ","m","e"),("モ","m","o"),
    ("ヤ","y","a"),("ユ","y","u"),("ヨ","y","o"),
    ("ラ","r","a"),("リ","r","i"),("ル","r","u"),("レ","r","e"),("ロ","r","o"),
    ("ワ","w","a"),("ヲ","w","o"),("ン","","N"),
    # 濁音・半濁音
    ("ガ","g","a"),("ギ","g","i"),("グ","g","u"),("ゲ","g","e"),("ゴ","g","o"),
    ("ザ","z","a"),("ジ","j","i"),("ズ","z","u"),("ゼ","z","e"),("ゾ","z","o"),
    ("ダ","d","a"),("ヂ","j","i"),("ヅ","z","u"),("デ","d","e"),("ド","d","o"),
    ("バ","b","a"),("ビ","b","i"),("ブ","b","u"),("ベ","b","e"),("ボ","b","o"),
    ("パ","p","a"),("ピ","p","i"),("プ","p","u"),("ペ","p","e"),("ポ","p","o"),
    # 拗音
    ("キャ","ky","a"),("キュ","ky","u"),("キョ","ky","o"),
    ("シャ","sh","a"),("シュ","sh","u"),("ショ","sh","o"),
    ("チャ","ch","a"),("チュ","ch","u"),("チョ","ch","o"),
    ("ニャ","ny","a"),("ニュ","ny","u"),("ニョ","ny","o"),
    ("ヒャ","hy","a"),("ヒュ","hy","u"),("ヒョ","hy","o"),
    ("ミャ","my","a"),("ミュ","my","u"),("ミョ","my","o"),
    ("リャ","ry","a"),("リュ","ry","u"),("リョ","ry","o"),
    ("ギャ","gy","a"),("ギュ","gy","u"),("ギョ","gy","o"),
    ("ジャ","j","a"), ("ジュ","j","u"), ("ジョ","j","o"),
    ("ビャ","by","a"),("ビュ","by","u"),("ビョ","by","o"),
    ("ピャ","py","a"),("ピュ","py","u"),("ピョ","py","o"),
    ("ファ","f","a"), ("フィ","f","i"), ("フェ","f","e"), ("フォ","f","o"),
    ("ウィ","w","i"), ("ウェ","w","e"), ("ウォ","w","o"),
    ("ヴァ","v","a"), ("ヴィ","v","i"), ("ヴ","v","u"), ("ヴェ","v","e"), ("ヴォ","v","o"),
    ("ティ","t","i"), ("テュ","ty","u"),
    ("ディ","d","i"), ("デュ","dy","u"),
    ("ツァ","ts","a"),("ツィ","ts","i"),("ツェ","ts","e"),("ツォ","ts","o"),
    ("スィ","s","i"),("ズィ","z","i"),
    ("ッ","","cl"),   # 促音
]
# カタカナ → (子音, 母音) lookup
_KATA2MORA: dict[str, tuple[str, str]] = {k:(c,v) for k,c,v in _MORA_LIST}

# ── データクラス ─────────────────────────────────────────────────────────────

@dataclass
class StyleInfo:
    name: str
    id:   int

@dataclass
class Speaker:
    name:   str
    uuid:   str
    styles: list[StyleInfo] = field(default_factory=list)

    def display_styles(self) -> list[tuple[str, int]]:
        """[(表示名, style_id)] のリストを返す。"""
        return [(f"{self.name}  [{s.name}]", s.id) for s in self.styles]

@dataclass
class Mora:
    text:             str
    consonant:        Optional[str]
    consonant_length: float
    vowel:            str
    vowel_length:     float
    pitch:            float

    @property
    def shape(self) -> str:
        """母音から口形状キーを返す。"""
        return _VOWEL_TO_SHAPE.get(self.vowel, "a")

    @property
    def duration(self) -> float:
        return self.consonant_length + self.vowel_length

@dataclass
class AccentPhrase:
    moras:       list[Mora]
    accent:      int
    pause_mora:  Optional[Mora] = None
    is_interrogative: bool = False

@dataclass
class AudioQuery:
    accent_phrases:     list[AccentPhrase]
    speed_scale:        float = 1.0
    pitch_scale:        float = 0.0
    intonation_scale:   float = 1.0
    volume_scale:       float = 1.0
    pre_phoneme_length: float = 0.1
    post_phoneme_length:float = 0.1
    output_sampling_rate: int = 24000
    output_stereo:      bool  = False

    @classmethod
    def from_dict(cls, d: dict) -> "AudioQuery":
        phrases = []
        for ph in d.get("accent_phrases", []):
            moras = [
                Mora(
                    text             = m.get("text",""),
                    consonant        = m.get("consonant"),
                    consonant_length = m.get("consonant_length") or 0.0,
                    vowel            = m.get("vowel","a"),
                    vowel_length     = m.get("vowel_length",0.0),
                    pitch            = m.get("pitch",0.0),
                )
                for m in ph.get("moras", [])
            ]
            pm_d  = ph.get("pause_mora")
            pm    = Mora(
                text             = pm_d.get("text",""),
                consonant        = pm_d.get("consonant"),
                consonant_length = pm_d.get("consonant_length") or 0.0,
                vowel            = pm_d.get("vowel","pau"),
                vowel_length     = pm_d.get("vowel_length",0.0),
                pitch            = 0.0,
            ) if pm_d else None
            phrases.append(AccentPhrase(
                moras       = moras,
                accent      = ph.get("accent_pos", ph.get("accent", 0)),
                pause_mora  = pm,
                is_interrogative = ph.get("is_interrogative", False),
            ))
        return cls(
            accent_phrases      = phrases,
            speed_scale         = d.get("speedScale",        1.0),
            pitch_scale         = d.get("pitchScale",        0.0),
            intonation_scale    = d.get("intonationScale",   1.0),
            volume_scale        = d.get("volumeScale",       1.0),
            pre_phoneme_length  = d.get("prePhonemeLength",  0.1),
            post_phoneme_length = d.get("postPhonemeLength", 0.1),
            output_sampling_rate= d.get("outputSamplingRate",24000),
            output_stereo       = d.get("outputStereo",      False),
        )

    def to_dict(self) -> dict:
        """API に POST する用の辞書を返す。"""
        phrases = []
        for ph in self.accent_phrases:
            moras = []
            for m in ph.moras:
                md: dict = {"text": m.text, "vowel": m.vowel,
                            "vowel_length": m.vowel_length, "pitch": m.pitch}
                if m.consonant is not None:
                    md["consonant"]        = m.consonant
                    md["consonant_length"] = m.consonant_length
                moras.append(md)
            phd: dict = {"moras": moras, "accent": ph.accent,
                         "is_interrogative": ph.is_interrogative}
            if ph.pause_mora:
                pm = ph.pause_mora
                phd["pause_mora"] = {
                    "text": pm.text, "vowel": pm.vowel,
                    "vowel_length": pm.vowel_length, "pitch": pm.pitch,
                    "consonant": pm.consonant, "consonant_length": pm.consonant_length,
                }
            phrases.append(phd)
        return {
            "accent_phrases":      phrases,
            "speedScale":          self.speed_scale,
            "pitchScale":          self.pitch_scale,
            "intonationScale":     self.intonation_scale,
            "volumeScale":         self.volume_scale,
            "prePhonemeLength":    self.pre_phoneme_length,
            "postPhonemeLength":   self.post_phoneme_length,
            "outputSamplingRate":  self.output_sampling_rate,
            "outputStereo":        self.output_stereo,
        }

# ── HTTP ヘルパー ─────────────────────────────────────────────────────────────

def _http_get(url: str, timeout_conn: int = 10, timeout_read: int = 30,
              retries: int = 3) -> bytes:
    """GET リクエスト（リトライ付き）。
    timeout は接続+読み取りの合算で urlopen に渡す（urllib は分離指定不可）。
    """
    last_err: Exception = ConnectionError("未接続")
    for _ in range(max(1, retries)):
        try:
            res = urllib.request.urlopen(url, timeout=timeout_conn + timeout_read)
            return res.read()
        except Exception as e:
            last_err = e
            time.sleep(1.0)
    raise last_err


def _http_post(url: str, data: bytes, content_type: str = "application/json",
               timeout_conn: int = 10, timeout_read: int = 300,
               retries: int = 3) -> bytes:
    """POST リクエスト（リトライ付き）。
    URLError / ConnectionResetError / RemoteDisconnected など
    すべての一時的エラーでリトライする（URLError に限定しない）。
    """
    last_err: Exception = ConnectionError("未接続")
    _RETRY_EXCEPTIONS = (
        urllib.error.URLError,
        ConnectionError,
        TimeoutError,
        OSError,
    )
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", content_type)
            res = urllib.request.urlopen(req, timeout=timeout_conn + timeout_read)
            return res.read()
        except _RETRY_EXCEPTIONS as e:
            last_err = e
            time.sleep(1.0)
        except Exception as e:
            # JSON パースエラー等の非一時的エラーは即失敗
            raise e
    raise last_err

# ── VoiceVox クライアント ─────────────────────────────────────────────────────

class VoiceVoxClient:
    """
    VoiceVox / SHAREVOX / VoiceVox NEMO の HTTP クライアント。
    Resolve_Script の api.py を参考に改良。
    """

    def __init__(self, host: str = DEFAULT_HOST,
                 engine: str = DEFAULT_ENGINE,
                 port: Optional[int] = None):
        self.host   = host
        self.engine = engine
        self.port   = port or ENGINES.get(engine, 50021)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── 接続 ─────────────────────────────────────────────────────────────────

    def get_version(self) -> Optional[str]:
        try:
            raw = _http_get(f"{self.base_url}/version", retries=2)
            return json.loads(raw).strip('"')
        except Exception:
            return None

    def is_available(self) -> bool:
        return self.get_version() is not None

    # ── 話者 ─────────────────────────────────────────────────────────────────

    def get_speakers(self) -> list[Speaker]:
        try:
            raw  = _http_get(f"{self.base_url}/speakers", retries=2)
            data = json.loads(raw)
            result: list[Speaker] = []
            for spk in data:
                styles = [StyleInfo(name=s["name"], id=s["id"])
                          for s in spk.get("styles", [])]
                result.append(Speaker(
                    name   = spk["name"],
                    uuid   = spk.get("speaker_uuid", ""),
                    styles = styles,
                ))
            return result
        except Exception:
            return []

    def flat_styles(self) -> list[tuple[str, int]]:
        """(表示名, style_id) のフラットリストを返す。"""
        result = []
        for spk in self.get_speakers():
            for display, sid in spk.display_styles():
                result.append((display, sid))
        return result

    # ── 音声合成 ─────────────────────────────────────────────────────────────

    def audio_query(self, text: str, speaker_id: int) -> AudioQuery:
        url  = (f"{self.base_url}/audio_query"
                f"?text={urllib.parse.quote(text)}&speaker={speaker_id}")
        raw  = _http_post(url, b"", timeout_read=30)
        return AudioQuery.from_dict(json.loads(raw))

    def synthesis(self, query: AudioQuery, speaker_id: int) -> bytes:
        url  = f"{self.base_url}/synthesis?speaker={speaker_id}"
        data = json.dumps(query.to_dict(), ensure_ascii=False).encode("utf-8")
        return _http_post(url, data, timeout_read=300)

    def synthesize(
        self,
        text: str,
        speaker_id: int,
        speed: float        = 1.0,
        pitch: float        = 0.0,
        intonation: float   = 1.0,
        volume: float       = 1.0,
        pre_silence: float  = 0.1,
        post_silence: float = 0.1,
    ) -> tuple[bytes, AudioQuery]:
        """テキスト → (WAVバイト列, AudioQuery)。タイミングデータも返す。"""
        q = self.audio_query(text, speaker_id)
        q.speed_scale         = speed
        q.pitch_scale         = pitch
        q.intonation_scale    = intonation
        q.volume_scale        = volume
        q.pre_phoneme_length  = pre_silence
        q.post_phoneme_length = post_silence
        wav = self.synthesis(q, speaker_id)
        return wav, q

# ── AnimType (口パクアニメタイプ) ─────────────────────────────────────────────

class AnimType:
    """
    Resolve_Script の anim.py を参考にした口パクアニメーションタイプ。
    - aiueo  : 子音=閉じ, 母音=対応口形状 (最も厳密)
    - aiueo2 : 子音=次の母音の口形状の半開き版 (滑らか)
    - aiueo3 : 子音=前の口形状を維持 (最も滑らか)
    - open   : 発話中は常に「あ」口 (シンプル)
    """
    AIUEO  = "aiueo"
    AIUEO2 = "aiueo2"
    AIUEO3 = "aiueo3"
    OPEN   = "open"
    ALL    = [AIUEO, AIUEO2, AIUEO3, OPEN]
    LABEL  = {AIUEO:"aiueo（標準）", AIUEO2:"aiueo2（滑らか）",
               AIUEO3:"aiueo3（最滑らか）", OPEN:"open（常開き）"}

# ── タイムライン生成 ──────────────────────────────────────────────────────────

def build_mora_timeline(
    audio_query: AudioQuery,
    fps: float,
    anim_type: str = AnimType.AIUEO2,
) -> list[str]:
    """
    AudioQuery からフレーム単位の口形状キーリストを生成。
    返値: ['closed', 'a', 'i', ...] (フレーム数分)

    anim_type に応じて子音フェーズの口形状を変える:
      aiueo  : 子音 → 'closed'
      aiueo2 : 子音 → 次の母音形状 (ブレンドなしだが次の母音を先取り)
      aiueo3 : 子音 → 前の母音形状
      open   : 発話全体 → 'a'
    """
    # (time_start, time_end, consonant_shape, vowel_shape) を収集
    events: list[tuple[float, float, str, str]] = []
    t = audio_query.pre_phoneme_length

    for phrase in audio_query.accent_phrases:
        for mora in phrase.moras:
            vowel_shape = mora.shape
            c_len = mora.consonant_length
            v_len = mora.vowel_length
            if c_len > 0:
                events.append((t, t + c_len, "**consonant**", vowel_shape))
                t += c_len
            events.append((t, t + v_len, vowel_shape, vowel_shape))
            t += v_len
        if phrase.pause_mora:
            pm = phrase.pause_mora
            v_len = pm.vowel_length
            events.append((t, t + v_len, "closed", "closed"))
            t += v_len

    t += audio_query.post_phoneme_length
    total = max(1, int(t * fps) + 1)
    timeline: list[str] = ["closed"] * total

    # 子音シェイプを anim_type で解決
    prev_shape = "closed"
    resolved: list[tuple[float, float, str]] = []
    for i, (start, end, c_shape, v_shape) in enumerate(events):
        if c_shape == "**consonant**":
            if anim_type == AnimType.AIUEO:
                shape = "closed"
            elif anim_type == AnimType.AIUEO2:
                shape = v_shape    # 次の母音を先取り
            elif anim_type == AnimType.AIUEO3:
                shape = prev_shape
            else:  # open
                shape = "a"
        else:
            shape = "a" if anim_type == AnimType.OPEN else v_shape
        resolved.append((start, end, shape))
        prev_shape = v_shape

    for start, end, shape in resolved:
        f_s = int(start * fps)
        f_e = int(end   * fps) + 1
        for f in range(f_s, min(f_e, total)):
            timeline[f] = shape

    return timeline
