#!/usr/bin/env python3

import os
import sys
from pathlib import Path
import json
import time
import tempfile
import requests
import subprocess
import re
import threading
import traceback
import datetime
from collections import deque
import shutil
import asyncio
import platform
import glob
import numpy as np

import pyaudio
import whisper
import edge_tts
from playsound3 import playsound

# === файлы ===
CURRENT_DIR = Path(__file__).parent
CONFIG_FILE = CURRENT_DIR / "config.json"
SYSTEM_PROMPT_FILE = CURRENT_DIR / "system_prompt.txt"
HISTORY_FILE = CURRENT_DIR / "history.json"
SHORTCUTS_FILE = CURRENT_DIR / "desktop_shortcuts.json"

# === определение системы ===
system = str(platform.system()).lower()

# === настройки по умолчанию ===
DEFAULT_CONFIG = {
    "OLLAMA_URL": "http://77.94.115.215:11434/api/generate",
    "MODEL_NAME": "qwen2.5-coder:latest",
    "MAX_HISTORY": 10,
    "SILENCE_TIMEOUT": 1.5,
    "FOLLOWUP_WINDOW": 5.0,
    "TRIGGER": ["ада", "а да", "а, да", "ага"],
    "WHISPER_MODEL": "base",
    "SAMPLE_RATE": 16000,
    "TTS_ENGINE": "edge-tts",  # edge-tts или pyttsx3
    "TTS_VOICE": "ru-RU-SvetlanaNeural",  # для edge-tts
    "TTS_SPEED": 1.3,  # скорость озвучивания (0.5-2.0)
}

# === звуки ===
SOUND_PATHS = {
    "trigger": str(CURRENT_DIR / "sounds" / "trigger.wav"),
    "think": str(CURRENT_DIR / "sounds" / "think.wav"),
    "idle": str(CURRENT_DIR / "sounds" / "idle.wav")
}


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print("Ошибка при загрузке config.json:", e)
    return cfg


async def speak_edge_tts(text, voice, speed):
    """Edge TTS озвучивание"""
    try:
        rate_percent = f"{int((speed-1)*100):+d}%"
        
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate_percent)
        
        tmp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_file.close()
        
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                with open(tmp_file.name, "ab") as f:
                    f.write(chunk["data"])
        
        playsound(tmp_file.name)
        os.remove(tmp_file.name)
        
    except Exception as e:
        print(f"❌ Ошибка Edge TTS: {e}")
        raise


def speak_pyttsx3(text, speed):
    """pyttsx3 озвучивание (fallback)"""
    try:
        import pyttsx3
        
        engine = pyttsx3.init()
        pyttsx_speed = int(150 + (speed - 1.0) * 50)
        engine.setProperty('rate', pyttsx_speed)
        engine.setProperty('volume', 1.0)
        
        if system == "windows":
            voices = engine.getProperty('voices')
            if len(voices) > 1:
                engine.setProperty('voice', voices[1].id)
        
        engine.say(text)
        engine.runAndWait()
        
    except Exception as e:
        print(f"❌ Ошибка pyttsx3: {e}")
        raise


def speak(text, tts_engine="edge-tts", tts_voice="ru-RU-SvetlanaNeural", tts_speed=1.3):
    """
    Озвучивание текста
    
    Параметры:
    - text: текст для озвучки
    - tts_engine: 'edge-tts' или 'pyttsx3'
    - tts_voice: голос (для edge-tts)
    - tts_speed: скорость (0.5-2.0)
    """
    try:
        print(f"🔊 Озвучка...")
        
        if tts_engine == "edge-tts":
            asyncio.run(speak_edge_tts(text, tts_voice, tts_speed))
        else:
            speak_pyttsx3(text, tts_speed)
        
    except Exception as e:
        print(f"❌ Ошибка озвучивания: {e}")


def play_sound(name):
    path = SOUND_PATHS.get(name)
    if path and os.path.exists(path):
        try:
            playsound(path)
            print(f"🔔 [звук: {name}]")
        except Exception as e:
            print(f"Ошибка воспроизведения: {e}")


def load_system_prompt(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def load_history(maxlen):
    if not HISTORY_FILE.exists():
        return deque(maxlen=maxlen)
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return deque(data, maxlen=maxlen)
            else:
                return deque(maxlen=maxlen)
    except Exception:
        return deque(maxlen=maxlen)


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(list(history), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("❌ Ошибка сохранения:", e)


def check_trigger(text, triggers):
    """Проверяет наличие триггера в тексте."""
    text_clean = re.sub(r'[^\w\s]', '', text).lower()
    text_clean = ' '.join(text_clean.split())
    
    for trigger in triggers:
        trigger_clean = re.sub(r'[^\w\s]', '', trigger).lower()
        if trigger_clean in text_clean:
            return trigger
    
    return None


def get_desktop_path():
    try:
        up = os.environ.get("USERPROFILE")
        if up:
            p = Path(up) / "Desktop"
            if p.exists():
                return str(p)
    except Exception:
        pass
    p = Path.home() / "Desktop"
    return str(p)


def update_shortcuts_desktop():
    if system != "windows":
        speak("Команда только для Windows", "edge-tts", "ru-RU-SvetlanaNeural", 1.3)
        return

    desktop = get_desktop_path()
    print(f"📁 Сканирую: {desktop}")
    shortcuts = []

    try:
        glob_lnk = glob.glob(os.path.join(desktop, "*.lnk"))
        glob_exe = glob.glob(os.path.join(desktop, "*.exe"))
        shortcuts.extend(glob_lnk)
        shortcuts.extend(glob_exe)
    except Exception as e:
        print("❌ Ошибка:", e)

    shortcuts = sorted(set(shortcuts))

    try:
        with open(SHORTCUTS_FILE, "w", encoding="utf-8") as f:
            json.dump(shortcuts, f, ensure_ascii=False, indent=2)
        msg = f"✅ Обновлено {len(shortcuts)} ярлыков"
        print(msg)
        speak(msg, "edge-tts", "ru-RU-SvetlanaNeural", 1.3)
    except Exception as e:
        print("❌ Ошибка:", e)


def launch_program_by_name_from_desktop(name: str):
    if system != "windows":
        return False

    if not SHORTCUTS_FILE.exists():
        speak("Обнови ярлыки", "edge-tts", "ru-RU-SvetlanaNeural", 1.3)
        return False

    try:
        with open(SHORTCUTS_FILE, "r", encoding="utf-8") as f:
            shortcuts = json.load(f)
    except Exception:
        return False

    name_lower = name.lower()
    matches = []
    for path in shortcuts:
        base = os.path.basename(path).lower()
        if name_lower in base or name_lower in os.path.splitext(base)[0]:
            matches.append(path)

    if not matches:
        speak("Не нашла приложение", "edge-tts", "ru-RU-SvetlanaNeural", 1.3)
        return False

    target = matches[0]
    try:
        speak(f"Запускаю {os.path.splitext(os.path.basename(target))[0]}", "edge-tts", "ru-RU-SvetlanaNeural", 1.3)
        subprocess.Popen(["powershell", "-NoProfile", "-Command", f"Start-Process -FilePath \"{target}\""], shell=False)
        return True
    except Exception:
        speak("Ошибка запуска", "edge-tts", "ru-RU-SvetlanaNeural", 1.3)
        return False


def llama(prompt, system_prompt, history=None, model_name="qwen2.5-coder:latest", url="http://77.94.115.215:11434/api/generate", timeout=60):
    history_text = ""
    if history:
        for turn in history:
            t = turn.get("time", "")
            u = turn.get("user", "")
            a = turn.get("assistant", "")
            history_text += f"[{t}] Пользователь: {u}\nИИ: {a}\n"

    full_prompt = f"{system_prompt}\n\n{history_text}\nПользователь: {prompt}\nИИ:"
    data = {"model": model_name, "prompt": full_prompt, "stream": False}

    print("🔌 Подключаюсь...")
    start_time = time.time()
    try:
        response = requests.post(url, json=data, stream=False, timeout=timeout)
        response.raise_for_status()
        text = ""
        for line in response.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
                chunk_text = chunk.get("response", "")
                if chunk_text:
                    text += chunk_text
            except Exception:
                pass
        print(f"✅ За {time.time() - start_time:.2f} сек")
        return text.strip()
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return ""


def check_ollama_connection(url="http://77.94.115.215:11434/api/tags"):
    try:
        r = requests.get(url, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def setup_whisper(model_name="base"):
    print(f"📥 Загружаю модель '{model_name}'...")
    try:
        model = whisper.load_model(model_name)
        print(f"✅ Модель загружена")
        return model
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return None


def recognize_audio_array(model, audio_array, trigger_hint="", sample_rate=16000):
    try:
        print(f"🎙️ Распознаю...")
        start_time = time.time()
        
        audio = audio_array.astype(np.float32) / 32768.0
        
        result = model.transcribe(
            audio,
            language="ru",
            verbose=False,
            fp16=False,
            initial_prompt=trigger_hint if trigger_hint else None
        )
        
        elapsed = time.time() - start_time
        text = result.get("text", "").strip()
        
        if text:
            print(f"✅ За {elapsed:.2f} сек: '{text}'")
        
        return text
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return ""


def listen_until_silence(model, device_index=None, silence_timeout=1.5, max_duration=20, sample_rate=16000, trigger_hint=""):
    """Слушает до тишины и распознаёт."""
    p = pyaudio.PyAudio()
    
    if device_index is None:
        device_index = p.get_default_input_device_info()['index']
    
    print(f"🎤 Говорите ({max_duration} сек)...")
    
    frames = []
    silence_frames = 0
    
    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=2048
        )
        
        start_time = time.time()
        
        while time.time() - start_time < max_duration:
            try:
                data = stream.read(2048, exception_on_overflow=False)
                frames.append(data)
                
                audio_array = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                if len(audio_array) > 0:
                    rms = np.sqrt(np.mean(audio_array ** 2))
                else:
                    rms = 0
                
                if rms < 500:
                    silence_frames += 1
                else:
                    silence_frames = 0
                
                level = int(max(0, min(20, rms / 300)))
                bar = "█" * level + "░" * (20 - level)
                sys.stdout.write(f"\r[{bar}] {rms:.0f}")
                sys.stdout.flush()
                
                if silence_frames > int(silence_timeout * 7.8):
                    if len(frames) > 10:
                        print("\n⏸️ Тишина")
                        break
                        
            except Exception as e:
                continue
        
        stream.stop_stream()
        stream.close()
        
    except Exception as e:
        print(f"\n❌ Ошибка потока: {e}")
        p.terminate()
        return ""
    
    p.terminate()
    
    if len(frames) < 5:
        print("⏭️ Короткая запись")
        return ""
    
    print("🔄 Объединяю аудио...")
    audio_bytes = b''.join(frames)
    audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
    
    print(f"📊 Аудиоданные: {len(audio_array)} samples ({len(audio_array)/16000:.2f} сек)")
    
    text = recognize_audio_array(model, audio_array, trigger_hint, sample_rate)
    
    return text


def listen_with_timeout(model, device_index=None, timeout=3.0, sample_rate=16000, trigger_hint=""):
    """Слушает в течение timeout секунд БЕЗ триггера."""
    p = pyaudio.PyAudio()
    
    if device_index is None:
        device_index = p.get_default_input_device_info()['index']
    
    frames = []
    has_sound = False
    
    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=2048
        )
        
        start_time = time.time()
        silence_frames = 0
        
        while time.time() - start_time < timeout:
            try:
                data = stream.read(2048, exception_on_overflow=False)
                frames.append(data)
                
                audio_array = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                if len(audio_array) > 0:
                    rms = np.sqrt(np.mean(audio_array ** 2))
                else:
                    rms = 0
                
                if rms > 500:
                    has_sound = True
                    silence_frames = 0
                else:
                    silence_frames += 1
                
                level = int(max(0, min(20, rms / 300)))
                bar = "█" * level + "░" * (20 - level)
                remaining = timeout - (time.time() - start_time)
                sys.stdout.write(f"\r[{bar}] {remaining:.1f}s")
                sys.stdout.flush()
                
                if has_sound and silence_frames > int(1.0 * 7.8):
                    print("\n✓ Заве��шено")
                    break
                        
            except Exception as e:
                continue
        
        stream.stop_stream()
        stream.close()
        
    except Exception as e:
        print(f"\n❌ Ошибка потока: {e}")
        p.terminate()
        return ""
    
    p.terminate()
    
    if not has_sound or len(frames) < 3:
        print("\n⏭️ Только молчание")
        return ""
    
    print("\n🔄 Обработка...")
    audio_bytes = b''.join(frames)
    audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
    
    text = recognize_audio_array(model, audio_array, trigger_hint, sample_rate)
    
    return text


def process_answer(answer: str, tts_engine, tts_voice, tts_speed):
    executed = False

    if system == "windows":
        for cmd in re.findall(r"\[\[\s*(?:powershell|ps)\s*:\s*(.*?)\s*\]\]", answer, re.I):
            print(f"⚙️ Выполняю: {cmd}")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd], shell=False)
            executed = True
    else:
        for cmd in re.findall(r"\[\[\s*bash\s*:\s*(.*?)\s*\]\]", answer, re.I):
            print(f"⚙️ Выполняю: {cmd}")
            subprocess.Popen(["bash", "-c", cmd], shell=False)
            executed = True

    clean = re.sub(r"\[\[.*?\]\]", "", answer).strip()

    if clean:
        print("🤖 Ассистент:", clean)
        speak(clean, tts_engine, tts_voice, tts_speed)


def main():
    config = load_config()
    OLLAMA_URL = config.get("OLLAMA_URL")
    MODEL_NAME = config.get("MODEL_NAME")
    MAX_HISTORY = int(config.get("MAX_HISTORY", 10))
    SILENCE_TIMEOUT = float(config.get("SILENCE_TIMEOUT", 1.5))
    FOLLOWUP_WINDOW = float(config.get("FOLLOWUP_WINDOW", 5.0))
    
    TRIGGERS = config.get("TRIGGER", ["ада"])
    if isinstance(TRIGGERS, str):
        TRIGGERS = [TRIGGERS]
    
    TRIGGER_HINT = " ".join(TRIGGERS)
    WHISPER_MODEL = config.get("WHISPER_MODEL", "base")
    
    # TTS настройки из конфига
    TTS_ENGINE = config.get("TTS_ENGINE", "edge-tts")
    TTS_VOICE = config.get("TTS_VOICE", "ru-RU-SvetlanaNeural")
    TTS_SPEED = float(config.get("TTS_SPEED", 1.3))

    print("\n" + "=" * 60)
    print("🎙️ ГОЛОСОВОЙ АССИСТЕНТ")
    print("=" * 60)

    print("\n🔍 Проверка Ollama...")
    if check_ollama_connection():
        print("✅ Ollama доступен")
    else:
        print("⚠️ Ollama может быть недоступен")

    print("\n📥 Инициализация Whisper...")
    model = setup_whisper(WHISPER_MODEL)
    if model is None:
        print("❌ Ошибка Whisper!")
        return

    system_prompt = load_system_prompt(SYSTEM_PROMPT_FILE)
    history = load_history(MAX_HISTORY)

    print(f"\n{'=' * 60}")
    print(f"✅ ГОТОВО!")
    print(f"🎙️ Триггеры: {', '.join(TRIGGERS)}")
    print(f"⏱️ Окно продолжения: {FOLLOWUP_WINDOW} сек")
    print(f"🔊 TTS: {TTS_ENGINE} (голос: {TTS_VOICE}, скорость: x{TTS_SPEED})")
    print(f"📚 История: {len(history)} записей")
    print(f"{'=' * 60}\n")

    play_sound("idle")

    try:
        while True:
            print("\n" + "=" * 60)
            print("💬 ПРОСЛУШИВАНИЕ (ЖДЁМ ТРИГГЕР)")
            print("=" * 60)
            
            user_raw = listen_until_silence(
                model, 
                device_index=None,
                silence_timeout=SILENCE_TIMEOUT,
                max_duration=20,
                trigger_hint=TRIGGER_HINT
            )
            
            if not user_raw or len(user_raw) < 2:
                print("⏭️ Повторите\n")
                continue

            print(f"\n👤 Вы: {user_raw}")
            
            found_trigger = check_trigger(user_raw, TRIGGERS)
            
            if not found_trigger:
                print(f"⏳ Жду триггер: {', '.join(TRIGGERS)}\n")
                continue
            
            print(f"✅ Триггер '{found_trigger}' найден!\n")
            
            user_time = datetime.datetime.now().strftime("%H:%M:%S")
            user_text_for_model = f"[{user_time}] {user_raw}"
            low = user_raw.lower()

            if system == "windows":
                if "обнови ярлыки" in low:
                    update_shortcuts_desktop()
                    play_sound("idle")
                    continue

            if "очисти" in low or "стоп" in low or "пока" in low:
                if "стоп" in low or "пока" in low:
                    print("👋 До свидания!")
                    speak("До свидания", TTS_ENGINE, TTS_VOICE, TTS_SPEED)
                    break
                else:
                    history = deque(maxlen=MAX_HISTORY)
                    save_history(history)
                    print("🗑️ Очищено")
                    speak("Память очищена", TTS_ENGINE, TTS_VOICE, TTS_SPEED)
                    continue

            print("⏳ Обрабатываю...")
            play_sound("think")

            try:
                answer = llama(user_text_for_model, system_prompt, history, MODEL_NAME, OLLAMA_URL)
            except Exception as e:
                print(f"❌ Ошибка: {e}")
                answer = ""

            if answer:
                process_answer(answer, TTS_ENGINE, TTS_VOICE, TTS_SPEED)
                history.append({
                    "time": user_time,
                    "user": user_raw,
                    "assistant": answer
                })
                if len(history) > MAX_HISTORY:
                    while len(history) > MAX_HISTORY:
                        history.popleft()
                save_history(history)

            play_sound("idle")

            # ОКНО ПРОДОЛЖЕНИЯ
            print(f"\n{'=' * 60}")
            print(f"⏱️ ОКНО ПРОДОЛЖЕНИЯ ({FOLLOWUP_WINDOW} сек)")
            print("Говорите БЕЗ триггера, или молчите для выхода")
            print(f"{'=' * 60}")

            followup_deadline = time.time() + FOLLOWUP_WINDOW

            while time.time() < followup_deadline:
                remaining_time = followup_deadline - time.time()
                
                if remaining_time <= 0:
                    print("\n⏱️ Время вышло - возврат к триггеру")
                    break

                continuation = listen_with_timeout(
                    model,
                    device_index=None,
                    timeout=min(remaining_time, 2.0),
                    trigger_hint=TRIGGER_HINT
                )

                if not continuation or len(continuation) < 2:
                    remaining = followup_deadline - time.time()
                    if remaining > 0:
                        print(f"🤫 Молчание ({remaining:.1f}s осталось)")
                        continue
                    else:
                        print("\n⏱️ Время вышло - возврат к триггеру")
                        break

                print(f"\n👤 Продолжение: {continuation}")

                low_cont = continuation.lower()
                if "стоп" in low_cont or "пока" in low_cont:
                    print("👋 До свидания!")
                    speak("До свидания", TTS_ENGINE, TTS_VOICE, TTS_SPEED)
                    sys.exit(0)

                user_time = datetime.datetime.now().strftime("%H:%M:%S")
                user_text_for_model = f"[{user_time}] {continuation}"

                print("⏳ Обрабатываю...")
                play_sound("think")

                try:
                    answer = llama(user_text_for_model, system_prompt, history, MODEL_NAME, OLLAMA_URL)
                except Exception as e:
                    print(f"❌ Ошибка: {e}")
                    answer = ""

                if answer:
                    process_answer(answer, TTS_ENGINE, TTS_VOICE, TTS_SPEED)
                    history.append({
                        "time": user_time,
                        "user": continuation,
                        "assistant": answer
                    })
                    if len(history) > MAX_HISTORY:
                        while len(history) > MAX_HISTORY:
                            history.popleft()
                    save_history(history)

                play_sound("idle")

                followup_deadline = time.time() + FOLLOWUP_WINDOW
                print(f"\n{'=' * 60}")
                print(f"⏱️ ОКНО ПРОДОЛЖЕНИЯ ({FOLLOWUP_WINDOW} сек)")
                print("Говорите дальше, или молчите для выхода")
                print(f"{'=' * 60}")

    except KeyboardInterrupt:
        print("\n\n⏸️ Остановлено")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        traceback.print_exc()
    finally:
        print("\n👋 Выход...")


if __name__ == "__main__":
    main()