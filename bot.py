import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.utils import get_random_id

import json
import os
import time
import requests
import traceback
from PIL import Image, ImageDraw, ImageFont

from flask import Flask
from threading import Thread


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def abspath(p):
    return p if os.path.isabs(p) else os.path.join(BASE_DIR, p)


CONFIG_PATH = abspath('config.json')
try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    print(f"Ошибка: config.json не найден: {CONFIG_PATH}")
    exit()
except json.JSONDecodeError as e:
    print(f"Ошибка: невалидный JSON: {e}")
    exit()

DAYS = CONFIG['days']
DIGITS = CONFIG['digits']

TOKEN = os.environ.get("VK_TOKEN")
GROUP_ID = os.environ.get("VK_GROUP_ID")

if not TOKEN:
    raise SystemExit("Ошибка: переменная VK_TOKEN не задана")
if not GROUP_ID:
    raise SystemExit("Ошибка: переменная VK_GROUP_ID не задана")

try:
    GROUP_ID = int(GROUP_ID)
except ValueError:
    raise SystemExit("Ошибка: VK_GROUP_ID должен быть числом")

vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()

user_states = {}
last_message = {}
processing = {}

DEDUP_WINDOW = 3.0


def log(*args):
    print("[BOT]", *args, flush=True)


app = Flask(__name__)


@app.route('/healthz')
def healthz():
    return {"status": "ok"}, 200


def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def is_subscribed(user_id):
    """Проверяет, подписан ли пользователь на сообщество. С логом ответа VK."""
    try:
        result = vk.groups.isMember(
            group_id=GROUP_ID,
            user_id=user_id
        )
        log(f"isMember ответ для user_id={user_id}: {result}")

        if isinstance(result, list) and len(result) > 0:
            member = result[0].get('member', 0)
            log(f"Поле member = {member} (1=подписан, 0=нет)")
            return member == 1

        log(f"Неожиданный формат ответа: {type(result)}")
        return False

    except Exception as e:
        log(f"Ошибка проверки подписки для {user_id}: {e}")
        traceback.print_exc()
        return False


def is_duplicate(user_id, text):
    now = time.time()
    prev = last_message.get(user_id)
    if prev and prev["text"] == text and (now - prev["time"]) < DEDUP_WINDOW:
        return True
    last_message[user_id] = {"text": text, "time": now}
    return False


def get_font(size):
    font_path = abspath("bubblefont.ttf")
    if os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception as e:
            log(f"Не удалось загрузить шрифт: {e}")
    return ImageFont.load_default()


def create_final_image(background_path, text_to_draw):
    bg_abs = abspath(background_path)
    if not os.path.exists(bg_abs):
        log(f"Фон не найден: {bg_abs}")
        return None

    try:
        base = Image.open(bg_abs).convert("RGBA")
    except Exception as e:
        log(f"Ошибка открытия фона: {e}")
        return None

    width, height = base.size
    txt_layer = Image.new('RGBA', base.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    font_size = max(10, int(height * 0.08 * 1.25))
    font = get_font(font_size)

    try:
        bbox = draw.textbbox((0, 0), text_to_draw, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except AttributeError:
        text_w, text_h = draw.textsize(text_to_draw, font=font)

    x = (width - text_w) / 2
    y = 40

    draw.text((x - 3, y - 3), text_to_draw, font=font, fill=(0, 0, 0, 150))
    draw.text((x, y), text_to_draw, font=font, fill=(255, 255, 255, 255))

    out = Image.alpha_composite(base, txt_layer)
    temp_path = os.path.join(BASE_DIR, f"result_{get_random_id()}.png")
    out.save(temp_path)
    log(f"Картинка сохранена: {temp_path} (шрифт {font_size}px)")
    return temp_path


def upload_photo_manual(file_path, max_attempts=3, delay=2):
    for attempt in range(1, max_attempts + 1):
        log(f"Попытка загрузки #{attempt}")

        try:
            upload_server = vk.photos.getMessagesUploadServer(group_id=GROUP_ID)
        except Exception as e:
            log(f"Ошибка получения upload server: {e}")
            time.sleep(delay)
            continue

        upload_url = upload_server['upload_url']

        resp_json = None
        try:
            with open(file_path, 'rb') as f:
                files = {'photo': ('photo.png', f, 'image/png')}
                resp = requests.post(upload_url, files=files, timeout=30)
            log(f"Ответ (photo) #{attempt}: {resp.status_code} | {resp.text[:200]}")
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = None
        except Exception as e:
            log(f"Ошибка запроса (photo) #{attempt}: {e}")

        if not resp_json or not resp_json.get('photo') or resp_json.get('photo') == '[]':
            try:
                with open(file_path, 'rb') as f:
                    files = {'file1': ('photo.png', f, 'image/png')}
                    resp = requests.post(upload_url, files=files, timeout=30)
                log(f"Ответ (file1) #{attempt}: {resp.status_code} | {resp.text[:200]}")
                try:
                    resp_json = resp.json()
                except Exception:
                    resp_json = None
            except Exception as e:
                log(f"Ошибка запроса (file1) #{attempt}: {e}")

        if resp_json and resp_json.get('photo') and resp_json['photo'] != '[]':
            try:
                save_result = vk.photos.saveMessagesPhoto(
                    photo=resp_json['photo'],
                    hash=resp_json.get('hash'),
                    server=resp_json.get('server'),
                    group_id=GROUP_ID
                )
                log(f"Фото сохранено с попытки #{attempt}: {save_result}")
                return save_result[0]
            except Exception as e:
                log(f"Ошибка saveMessagesPhoto #{attempt}: {e}")

        if attempt < max_attempts:
            log(f"Ждём {delay} сек перед повтором...")
            time.sleep(delay)

    log("Все попытки загрузки исчерпаны.")
    return None


def send_message(user_id, message, attachment=None):
    try:
        vk.messages.send(
            user_id=user_id,
            random_id=get_random_id(),
            message=message,
            attachment=attachment,
            group_id=GROUP_ID
        )
    except Exception as e:
        log(f"Ошибка отправки сообщения: {e}")


def main():
    log("Бот запущен. VkLongPoll слушает...")
    log(f"BASE_DIR = {BASE_DIR}")
    log(f"GROUP_ID = {GROUP_ID}")

    longpoll = VkLongPoll(vk_session, group_id=GROUP_ID)

    for event in longpoll.listen():
        if event.type != VkEventType.MESSAGE_NEW or not event.to_me:
            continue
        if not event.text:
            continue

        user_id = event.user_id
        text = event.text.strip().lower()

        if processing.get(user_id):
            log(f"Пользователь {user_id} занят генерацией, пропускаю: {text!r}")
            continue

        if is_duplicate(user_id, text):
            log(f"Пропускаю дубликат: user={user_id}, text={text!r}")
            continue

        log(f"Сообщение от {user_id}: {text!r}")

        # ---------- ПРОВЕРКА ПОДПИСКИ ----------
        subscribed = is_subscribed(user_id)
        log(f"Результат проверки подписки для {user_id}: {subscribed}")

        if not subscribed:
            log(f"Пользователь {user_id} не подписан — отказ")
            send_message(
                user_id,
                "⚠️ Чтобы пользоваться ботом, подпишитесь на сообщество."
            )
            continue

        # Шаг 1: старт
        if text == "начать":
            user_states.pop(user_id, None)
            send_message(user_id, "📅 Введите число вашего рождения (от 1 до 31):")
            continue

        state = user_states.get(user_id)

        # Шаг 3: цифра дома (0–9)
        if state is not None and "day_index" in state:
            if not text.isdigit() or not (0 <= int(text) <= 9):
                send_message(user_id, "Пожалуйста, введите одну цифру от 0 до 9.")
                continue

            processing[user_id] = True

            try:
                digit_str = text
                day_index = state["day_index"]
                word_from_day = DAYS[day_index]
                image_path = DIGITS[digit_str]['image']

                send_message(user_id, "🎨 Генерирую картинку, подождите пару секунд...")
                log(f"Генерирую: фон={image_path}, текст={word_from_day!r}")

                try:
                    final_image_path = create_final_image(image_path, word_from_day)
                except Exception as e:
                    log("EXCEPTION в create_final_image:")
                    traceback.print_exc()
                    send_message(user_id, f"❗ Ошибка генерации: {e}")
                    user_states.pop(user_id, None)
                    continue

                if not final_image_path:
                    send_message(user_id, "❗ Ошибка! Не удалось найти фоновый рисунок.")
                    user_states.pop(user_id, None)
                    continue

                photo = upload_photo_manual(final_image_path, max_attempts=3, delay=2)

                if not photo:
                    send_message(
                        user_id,
                        "😔 Не удалось загрузить картинку из-за сбоя ВКонтакте.\n"
                        "Попробуйте ещё раз отправить последнюю цифру дома."
                    )
                    if os.path.exists(final_image_path):
                        os.remove(final_image_path)
                    continue

                owner_id = photo['owner_id']
                photo_id = photo['id']
                access_key = photo.get('access_key', '')
                attachment = f'photo{owner_id}_{photo_id}'
                if access_key:
                    attachment += f'_{access_key}'

                send_message(user_id, "", attachment=attachment)

                if os.path.exists(final_image_path):
                    os.remove(final_image_path)

                user_states.pop(user_id, None)

            finally:
                processing[user_id] = False

            continue

        # Шаг 2: день месяца (1–31)
        if text.isdigit():
            day_num = int(text)
            if 1 <= day_num <= 31:
                user_states[user_id] = {"day_index": day_num - 1}
                send_message(
                    user_id,
                    "🏠 Введите последнюю цифру номера вашего дома (от 0 до 9):"
                )
            else:
                send_message(user_id, "Пожалуйста, введите число от 1 до 31.")
        else:
            send_message(user_id, "Пожалуйста, введите число от 1 до 31.")


if __name__ == "__main__":
    http_thread = Thread(target=run_http_server, daemon=True)
    http_thread.start()
    log("HTTP-сервер запущен для health check")

    while True:
        try:
            main()
        except Exception as e:
            log(f"MAIN УПАЛ: {e}")
            traceback.print_exc()
            log("Перезапуск через 5 секунд...")
            time.sleep(5)
