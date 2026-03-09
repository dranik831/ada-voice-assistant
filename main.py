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
import speech_recognition as sr
import threading
import traceback
import datetime
from collections import deque
import shutil
from gtts import gTTS
from playsound3 import playsound
import platform
import audioop
import glob

# === файлы ===
CURRENT_DIR = Path(__file__).parent
CONFIG_FILE = CURRENT_DIR / "config.json"
SYSTEM_PROMPT_FILE = CURRENT_DIR / "system_prompt.txt"
HISTORY_FILE = CURRENT_DIR / "history.json"
SHORTCUTS_FILE = CURRENT_DIR / "desktop_shortcuts.json"

# === определение системы ===
system = str(platform.system()).lower()  # 'windows', 'linux', 'darwin'

# === настройки по умолчанию ===
DEFAULT_CONFIG = {
    "OLLAMA_URL": "http://77.94.115.215:11434/api/generate",
    "MODEL_NAME": "qwen2.5-coder:latest",
    "MAX_HISTORY": 10,
    "SILENCE_TIMEOUT": 2.0,       # время тишины, считаем что пользователь закончил речь
    "FOLLOWUP_WINDOW": 5.0,       # окно для быстрых продолжений
    "TRIGGER": "ада",             # триггерное слово (нижний регистр)
    "ENERGY_THRESHOLD": 400,      # базовый порог энергии для recognizer
    "PAUSE_THRESHOLD": 0.8,
    "DYNAMIC_ENERGY_THRESHOLD": True,
    "PHRASE_TIME_LIMIT": 20       # макс длина одной записи
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


def speak(text, lang="ru"):
    """Озвучиваем текст через gTTS"""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        tts = gTTS(text=text, lang=lang)
        tts.save(tmp.name)
        try:
            playsound(tmp.name)
        except Exception:
            # fallback: system sound utils
            if shutil.which("canberra-gtk-play"):
                subprocess.run(["canberra-gtk-play", "-f", tmp.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "windows":
                subprocess.run(["powershell", "-c", f"(New-Object Media.SoundPlayer '{tmp.name}').PlaySync();"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                print("Не могу воспроизвести звук, файл:", tmp.name)
        os.remove(tmp.name)
        print("Текст для озвучки:", text)
    except Exception as e:
        print("Ошибка озвучивания:", e)


def play_sound(name):
    """Проигрывает wav или системный звук"""
    path = SOUND_PATHS.get(name)
    if path and os.path.exists(path):
        try:
            playsound(path)
            print(f"[звук: {name}]")
        except Exception as e:
            print(f"Ошибка воспроизведения звука {name}: {e}")
    else:
        fallback = {
            "trigger": "message-new-instant",
            "think": "service-login",
            "idle": "complete"
        }.get(name, "bell")
        if shutil.which("canberra-gtk-play"):
            subprocess.run(["canberra-gtk-play", "-i", fallback], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # на Windows/других платформах просто логируем
            print(f"[звук: {name}] (файл не найден)")


def run_bash_command(command: str):
    def worker(cmd):
        try:
            if system == "windows":
                # используем powershell для совместимости
                subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["bash", "-lc", cmd],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("Ошибка выполнения команды:", e)

    th = threading.Thread(target=worker, args=(command,))
    th.daemon = True
    th.start()


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
        print("Ошибка сохранения истории:", e)


# === Desktop shortcuts scanning & launching (Windows only) ===
def get_desktop_path():
    # try USERPROFILE/Desktop, fallback to home/Desktop
    try:
        up = os.environ.get("USERPROFILE")
        if up:
            p = Path(up) / "Desktop"
            if p.exists():
                return str(p)
    except Exception:
        pass
    # fallback
    p = Path.home() / "Desktop"
    return str(p)


def update_shortcuts_desktop():
    """
    Сканируем только рабочий стол пользователя на предмет .lnk и .exe,
    сохраняем список в SHORTCUTS_FILE (JSON).
    """
    if system != "windows":
        print("Обновление ярлыков доступно только на Windows.")
        speak("Команда доступна только на Windows")
        return

    desktop = get_desktop_path()
    print("Сканирую рабочий стол:", desktop)
    shortcuts = []

    # ищем .lnk и .exe прямо на рабочем столе (не рекурсивно)
    try:
        glob_lnk = glob.glob(os.path.join(desktop, "*.lnk"))
        glob_exe = glob.glob(os.path.join(desktop, "*.exe"))
        shortcuts.extend(glob_lnk)
        shortcuts.extend(glob_exe)
    except Exception as e:
        print("Ошибка при поиске ярлыков:", e)

    # убираем дубли и сортируем
    shortcuts = sorted(set(shortcuts))

    try:
        with open(SHORTCUTS_FILE, "w", encoding="utf-8") as f:
            json.dump(shortcuts, f, ensure_ascii=False, indent=2)
        msg = f"Обновлено {len(shortcuts)} ярлыков/программ на рабочем столе."
        print(msg)
        speak(msg)
    except Exception as e:
        print("Ошибка при сохранении списка ярлыков:", e)
        speak("Не удалось сохранить список ярлыков")


def launch_program_by_name_from_desktop(name: str):
    """
    Ищем совпадение по имени файла на рабочем столе и запускаем через Start-Process.
    Ищем по подстроке в имени файла (без учёта регистра).
    """
    if system != "windows":
        print("Запуск программ с рабочего стола работает только на Windows.")
        speak("Команда доступна только на Windows")
        return False

    if not SHORTCUTS_FILE.exists():
        print("Список ярлыков не найден. Сначала обнови ярлыки.")
        speak("Сначала обнови ярлыки")
        return False

    try:
        with open(SHORTCUTS_FILE, "r", encoding="utf-8") as f:
            shortcuts = json.load(f)
    except Exception as e:
        print("Не удалось прочитать файл ярлыков:", e)
        speak("Не удалось прочитать файл ярлыков")
        return False

    name_lower = name.lower()
    matches = []
    for path in shortcuts:
        base = os.path.basename(path).lower()
        # ищем по подстроке без расширения
        if name_lower in base or name_lower in os.path.splitext(base)[0]:
            matches.append(path)

    if not matches:
        print("Совпадений не найдено для:", name)
        speak("Не нашла такое приложение на рабочем столе")
        return False

    # если несколько совпадений — берем первое; можно улучшить диалогом
    target = matches[0]
    try:
        # используем PowerShell Start-Process — корректно откроет .lnk и .exe
        print(f"Запускаю: {target}")
        speak(f"Запускаю {os.path.splitext(os.path.basename(target))[0]}")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", f"Start-Process -FilePath \"{target}\""], shell=False)
        return True
    except Exception as e:
        print("Ошибка при запуске:", e)
        speak("Не получилось запустить приложение")
        return False


# === Ollama interaction ===
def llama(prompt, system_prompt, history=None, model_name="qwen2.5-coder:latest", url="http://77.94.115.216:11434/api/generate", timeout=60):
    """
    Отправляет запрос в Ollama. Работает потоково (stream=True) и собирает текст.
    В prompt уже должна быть вставлена временная метка и текст пользователя.
    """
    history_text = ""
    if history:
        for turn in history:
            t = turn.get("time", "")
            u = turn.get("user", "")
            a = turn.get("assistant", "")
            history_text += f"[{t}] Пользователь: {u}\nИИ: {a}\n"

    full_prompt = f"{system_prompt}\n\n{history_text}\nПользователь: {prompt}\nИИ:"
    data = {"model": model_name, "prompt": full_prompt, "stream": False}

    print("Подключаюсь к модели...")
    start_time = time.time()
    try:
        response = requests.post(url, json=data, stream=False, timeout=timeout)
        response.raise_for_status()
        text = ""
        first_chunk_received = False
        last_update = time.time()
        for line in response.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                try:
                    piece = line.decode("utf-8", errors="ignore")
                    text += piece
                except Exception:
                    continue
            else:
                chunk_text = chunk.get("response") or chunk.get("text") or chunk.get("content") or ""
                if chunk_text:
                    if not first_chunk_received:
                        print(f"Первый кусок ответа через {time.time() - start_time:.2f} сек")
                        first_chunk_received = True
                    text += chunk_text
                    if time.time() - last_update > 0.5:
                        print(f"Получено {len(text)} символов...")
                        last_update = time.time()
        print(f"Ответ получен за {time.time() - start_time:.2f} сек")
        return text.strip()
    except requests.exceptions.Timeout:
        return "Извините, запрос занял слишком много времени."
    except Exception as e:
        print("Ошибка при запросе к модели:", e)
        return f"Произошла ошибка при обращении к модели: {e}"


def check_ollama_connection(url="http://77.94.115.216:11434/api/tags"):
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            print("Ollama доступен")
            return True
        else:
            print("Ollama ответил кодом", r.status_code)
            return False
    except Exception as e:
        print("Ollama недоступен:", e)
        return False


# === распознавание речи ===
def list_available_microphones():
    print("\nДоступные устройства записи:")
    try:
        names = sr.Microphone.list_microphone_names()
        for i, n in enumerate(names):
            print(f"  {i}: {n}")
    except Exception as e:
        print("Не удалось получить список микрофонов:", e)
    print()


def setup_microphone(recognizer, config):
    """
    Короткая надёжная калибровка микрофона.
    Возвращает объект sr.Microphone или None.
    """
    try:
        mic = sr.Microphone()
    except Exception as e:
        print("Микрофон не найден:", e)
        list_available_microphones()
        return None

    print("Калибрую микрофон (коротко)...")
    try:
        with mic as source:
            # тихо прячем лишний stderr от библиотек
            with open(os.devnull, "w") as devnull:
                old_stderr = sys.stderr
                sys.stderr = devnull
                try:
                    recognizer.dynamic_energy_threshold = False
                    recognizer.energy_threshold = int(config.get("ENERGY_THRESHOLD", 400))
                    recognizer.adjust_for_ambient_noise(source, duration=0.8)
                finally:
                    sys.stderr = old_stderr

            print("Калибровка завершена. Порог энергии:", recognizer.energy_threshold)

    except Exception as e:
        print("Ошибка калибровки:", e)

    recognizer.pause_threshold = float(config.get("PAUSE_THRESHOLD", 0.8))
    recognizer.dynamic_energy_threshold = bool(config.get("DYNAMIC_ENERGY_THRESHOLD", True))
    return mic


def monitor_microphone_level(recognizer, microphone, duration=0.15):
    """
    Быстро слушает микрофон и возвращает RMS уровень.
    duration небольшое (0.1-0.25) для отзывчивости индикатора.
    """
    if microphone is None:
        return 0
    try:
        with microphone as source:
            audio = recognizer.listen(source, timeout=duration, phrase_time_limit=duration)
            raw = audio.get_raw_data()
            width = getattr(audio, "sample_width", 2)
            return audioop.rms(raw, width)
    except Exception:
        return 0


def draw_sound_bar(level, max_level=5000, width=24):
    """
    Рисует полоску уровня звука в одной строке.
    Простая, кроссплатформенная.
    """
    norm = min(level / max_level, 1.0) if max_level > 0 else 0
    filled = int(norm * width)
    bar = "█" * filled + "░" * (width - filled)
    sys.stdout.write(f"\r🎤 Ожидание триггера [{bar}] ")
    sys.stdout.flush()


def listen_for_trigger(recognizer, microphone, trigger_phrase, energy_threshold=400, pause_threshold=0.8, timeout=1.5):
    """
    Слушаем коротко и проверяем, есть ли триггерная фраза.
    Возвращает True если триггер найден.
    """
    if microphone is None:
        return False
    recognizer.energy_threshold = energy_threshold
    recognizer.pause_threshold = pause_threshold
    recognizer.dynamic_energy_threshold = True
    try:
        with microphone as source:
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=3)
        text = recognizer.recognize_google(audio, language="ru-RU").lower()
        print("\nТриггер распознан:", text)
        if trigger_phrase in text:
            return True
    except sr.WaitTimeoutError:
        pass
    except sr.UnknownValueError:
        pass
    except sr.RequestError as e:
        print("Ошибка сервиса распознации:", e)
    except Exception as e:
        print("Ошибка при прослушивании триггера:", e)
    return False


def record_until_silence(recognizer, microphone, silence_timeout=2.0, phrase_time_limit=20):
    """
    Записываем речь до момента тишины (silence_timeout).
    Возвращаем распознанный текст (строка) или пустую строку при неразборчивости.
    """
    if microphone is None:
        return ""
    full_text = ""
    last_activity = time.time()
    start = time.time()
    max_total = phrase_time_limit + 2.0
    print("Запись... Говорите (макс длительность записи: {} сек)".format(phrase_time_limit))
    while True:
        try:
            with microphone as source:
                audio = recognizer.listen(source, timeout=silence_timeout, phrase_time_limit=phrase_time_limit)
            try:
                text = recognizer.recognize_google(audio, language="ru-RU").strip()
            except sr.UnknownValueError:
                text = ""
            except sr.RequestError as e:
                print("Ошибка сервиса распознавания:", e)
                return ""
            if text:
                print("Распознано:", text)
                full_text += (" " + text) if full_text else text
                last_activity = time.time()
            else:
                if time.time() - last_activity >= silence_timeout:
                    break
        except sr.WaitTimeoutError:
            if full_text and (time.time() - last_activity) >= silence_timeout:
                break
            if not full_text:
                break
        except Exception as e:
            print("Ошибка записи:", e)
            break
        if time.time() - start > max_total:
            break
    return full_text.strip()


def process_answer(answer: str):
    executed = False

    # === PowerShell (ТОЛЬКО Windows) ===
    if system == "windows":
        for cmd in re.findall(r"\[\[\s*(?:powershell|ps)\s*:\s*(.*?)\s*\]\]", answer, re.I):
            print(f"Выполняю PowerShell-команду: {cmd}")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", cmd],
                shell=False
            )
            executed = True

    # === Bash (Linux / macOS) ===
    else:
        for cmd in re.findall(r"\[\[\s*bash\s*:\s*(.*?)\s*\]\]", answer, re.I):
            print(f"Выполняю bash-команду: {cmd}")
            subprocess.Popen(
                ["bash", "-c", cmd],
                shell=False
            )
            executed = True

    # === очистка текста ===
    clean = re.sub(r"\[\[.*?\]\]", "", answer).strip()

    if clean:
        print("Ассистент:", clean)
        speak(clean)

    if not executed:
        print("(Команд для этой ОС не найдено)")


def main():
    

    config = load_config()
    OLLAMA_URL = config.get("OLLAMA_URL")
    MODEL_NAME = config.get("MODEL_NAME")
    MAX_HISTORY = int(config.get("MAX_HISTORY", 10))
    SILENCE_TIMEOUT = float(config.get("SILENCE_TIMEOUT", 2.0))
    FOLLOWUP_WINDOW = float(config.get("FOLLOWUP_WINDOW", 5.0))
    TRIGGER = config.get("TRIGGER", "ада").lower()
    ENERGY_THRESHOLD = int(config.get("ENERGY_THRESHOLD", 400))
    PAUSE_THRESHOLD = float(config.get("PAUSE_THRESHOLD", 0.8))
    DYNAMIC = bool(config.get("DYNAMIC_ENERGY_THRESHOLD", True))
    PHRASE_TIME_LIMIT = int(config.get("PHRASE_TIME_LIMIT", 20))

    print("🔍 Проверка подключения к Ollama...")
    if not check_ollama_connection():
        print("Не могу подключиться к Ollama. Убедитесь, что он запущен и доступен по адресу:", OLLAMA_URL)
        print("Запуск: ollama serve")
        # продолжаем — может понадобиться оффлайн-тест

    print("Проверка звуковых файлов:")
    for n, p in SOUND_PATHS.items():
        print(f"  {n}: {p} {'(найден)' if os.path.exists(p) else '(не найден)'}")

    recognizer = sr.Recognizer()
    microphone = setup_microphone(recognizer, config)
    if microphone is None:
        print("Не получилось инициализировать микрофон. Выход.")
        return

    system_prompt = load_system_prompt(SYSTEM_PROMPT_FILE)
    history = load_history(MAX_HISTORY)

    print("\n" + "=" * 40)
    print("=== Голосовой ассистент ===")
    print("=== Триггер:", TRIGGER, "===")
    print("=" * 40 + "\n")

    play_sound("idle")

    waiting_trigger = True

    try:
        while True:
            if waiting_trigger:
                # обновляем индикатор уровня звука
                level = monitor_microphone_level(recognizer, microphone, duration=0.15)
                draw_sound_bar(level, max_level=4000, width=28)

                # слушаем короткий кусок на триггер
                try:
                    if listen_for_trigger(recognizer, microphone, TRIGGER, ENERGY_THRESHOLD, PAUSE_THRESHOLD, timeout=0.8):
                        # переводим строку, чтобы не портить индикатор
                        sys.stdout.write("\n")
                        play_sound("trigger")
                        print("Триггер обнаружен. Начинаю запись запроса...")
                        waiting_trigger = False
                    else:
                        # короткая пауза, чтобы CPU не жрал весь цикл
                        time.sleep(0.05)
                        continue
                except Exception as e:
                    print("Ошибка при прослушивании триггера:", e)
                    time.sleep(0.2)
                    continue
            else:
                # Запись запросa до тишины
                user_raw = record_until_silence(recognizer, microphone, SILENCE_TIMEOUT, PHRASE_TIME_LIMIT)
                if not user_raw:
                    print("Не распознано или молчание — возвращаемся к ожиданию триггера")
                    waiting_trigger = True
                    play_sound("idle")
                    continue

                user_time = datetime.datetime.now().strftime("%H:%M:%S")
                user_text_for_model = f"[{user_time}] {user_raw}"

                # команды управления (на русском) — раньше были очистка и стоп
                low = user_raw.lower()

                # --- Новый: обновление ярлыков с рабочего стола (Windows only) ---
                if system == "windows":
                    if any(phrase in low for phrase in ("обнови ярлыки", "обновить ярлыки", "обнови ярлыки на рабочем столе")):
                        update_shortcuts_desktop()
                        waiting_trigger = True
                        play_sound("idle")
                        continue

                    # команды запуска приложений с рабочего стола: "запусти X", "открой X", "открыть X"
                    m = re.search(r"\b(?:запусти приложение|запусти программу)\b\s*(.+)", low)
                    if m:
                        target_name = m.group(1).strip()
                        # иногда распознавание добавляет пробелы/пунктуацию; чистим
                        target_name = re.sub(r"[^\w\s\-\._]", " ", target_name).strip()
                        launched = launch_program_by_name_from_desktop(target_name)
                        # если запустили — не слать запрос в модель
                        waiting_trigger = True
                        play_sound("idle")
                        continue

                # существующие команды управления
                if "очисти память" in low or "очистить память" in low or "очисти историю" in low or "очистить историю" in low:
                    history = deque(maxlen=MAX_HISTORY)
                    save_history(history)
                    print("Память очищена.")
                    speak("Память очищена.")
                    waiting_trigger = True
                    play_sound("idle")
                    continue
                if "стоп" in low or "выключись" in low or "заверши" in low:
                    print("Команда завершения получена. Выключаюсь.")
                    speak("До свидания")
                    play_sound("idle")
                    break

                print(f"Запрос ({user_time}): {user_raw}")

                # сигнал что думаем
                play_sound("think")
                print("Отправляю в модель...")

                answer = ""
                try:
                    answer = llama(user_text_for_model, system_prompt, history, MODEL_NAME, OLLAMA_URL)
                except Exception as e:
                    print("Ошибка при обращении к модели:", e)
                    traceback.print_exc()
                    answer = f"Произошла ошибка при обращении к модели: {e}"

                if not answer:
                    print("Пустой ответ от модели")
                    speak("Не получил ответа от модели")
                else:
                    # обработка ответа (включая исполнение bash/powershell)
                    process_answer(answer)
                    # сохраняем в историю
                    history.append({
                        "time": user_time,
                        "user": user_raw,
                        "assistant": answer
                    })
                    # тримим по maxlen
                    if len(history) > MAX_HISTORY:
                        while len(history) > MAX_HISTORY:
                            history.popleft()
                    save_history(history)
                    print("Запись в историю выполнена.")

                # окно быстрого продолжения
                print(f"Окно продолжения ({FOLLOWUP_WINDOW} сек). Говорите, если хотите продолжить.")
                follow_deadline = time.time() + FOLLOWUP_WINDOW
                continued = False
                while time.time() < follow_deadline:
                    # показываем индикатор и слушаем короткие сообщения
                    level = monitor_microphone_level(recognizer, microphone, duration=0.15)
                    draw_sound_bar(level, max_level=4000, width=28)
                    cont = record_until_silence(recognizer, microphone, SILENCE_TIMEOUT, phrase_time_limit=PHRASE_TIME_LIMIT)
                    if not cont:
                        continue
                    # если что-то сказали — продлеваем окно
                    cont_time = datetime.datetime.now().strftime("%H:%M:%S")
                    cont_for_model = f"[{cont_time}] {cont}"
                    print("\nПродолжение:", cont)
                    play_sound("think")
                    try:
                        ans2 = llama(cont_for_model, system_prompt, history, MODEL_NAME, OLLAMA_URL)
                    except Exception as e:
                        print("Ошибка при обращении к модели (продолжение):", e)
                        ans2 = f"Произошла ошибка при обращении к модели: {e}"
                    if ans2:
                        process_answer(ans2)
                        history.append({
                            "time": cont_time,
                            "user": cont,
                            "assistant": ans2
                        })
                        if len(history) > MAX_HISTORY:
                            while len(history) > MAX_HISTORY:
                                history.popleft()
                        save_history(history)
                        continued = True
                        follow_deadline = time.time() + FOLLOWUP_WINDOW
                    else:
                        speak("Не получил ответа")
                # возвращаемся к ожиданию триггера
                play_sound("idle")
                print("Возврат в режим ожидания триггера\n")
                waiting_trigger = True

    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
    except Exception as e:
        print("Критическая ошибка:", e)
        traceback.print_exc()
        try:
            speak("Произошла критическая ошибка, завершаю работу.")
        except Exception:
            pass


if __name__ == "__main__":
    main()
