import datetime
import os
import time
import threading
import requests
import xml.etree.ElementTree as ET
import sqlite3
from dotenv import load_dotenv
from openai import OpenAI
from flask import Flask, request, jsonify
import telebot
from telebot import types

load_dotenv()

app = Flask(__name__)

ATS_TOKEN = os.environ.get('ATS_TOKEN', '')
phone_ats = int(os.environ.get('PHONE_ATS', '7777777777'))
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
DB_FILE = os.environ.get('DB_FILE', 'db.db')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)
apiUrlInstance = os.environ.get('GREEN_API_URL', '')
idInstance = os.environ.get('GREEN_API_ID_INSTANCE', '')
apiTokenInstance = os.environ.get('GREEN_API_TOKEN', '')
phoneNumber = int(os.environ.get('PHONE_NUMBER', '7777777777'))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '10'))
SUBSCRIPTION_LIFETIME = 86400
RENEW_THRESHOLD = 300
ADMIN_ID = int(os.environ.get('ADMIN_ID', '0'))
HOST = os.environ.get('WEBHOOK_HOST', '')
FLASK_PORT = int(os.environ.get('FLASK_PORT', '80'))
RECORDS_DIR = os.environ.get('RECORDS_DIR', 'records')


def init_db():
    """Создание структуры БД при первом запуске и инициализация настроек."""
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Таблица с настройками работы системы
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT,
            status_analyze INTEGER DEFAULT 0,
            last_time_got_token TEXT
        )
        """
    )

    # Таблица с записями звонков и сгенерированными сообщениями
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT,
            ext_tracking_id TEXT UNIQUE,
            transcript TEXT DEFAULT '',
            generated_message TEXT DEFAULT '',
            sent_to_user INTEGER DEFAULT 0
        )
        """
    )

    # Гарантируем наличие одной строки настроек
    cursor.execute("SELECT COUNT(*) FROM settings")
    count = cursor.fetchone()[0]
    if count == 0:
        cursor.execute(
            "INSERT INTO settings (token, status_analyze, last_time_got_token) VALUES (?, ?, ?)",
            (None, 0, None),
        )

    conn.commit()
    conn.close()


def init_storage():
    """Создание служебных директорий (для записей звонков и пр.)."""
    os.makedirs(RECORDS_DIR, exist_ok=True)

menu_markup = types.InlineKeyboardMarkup(row_width=1)
back_menu = types.InlineKeyboardMarkup(row_width=1)
delete_markup = types.InlineKeyboardMarkup(row_width=1)
update = types.InlineKeyboardButton('🔄Обновить меню', callback_data='update')
update_subs = types.InlineKeyboardButton('🔄Обновить подписку', callback_data='update_subs')
get_code_wp = types.InlineKeyboardButton('🆒Получить код для WP', callback_data='get_code_wp')
on_off_system = types.InlineKeyboardButton('🔛Включить/выключить анализ разговоров', callback_data='on_off_analyze')
back_menu_button = types.InlineKeyboardButton('🔙Вернуться в меню', callback_data='back_menu')
delete_button = types.InlineKeyboardButton('❌Удалить сообщение', callback_data='delete_msg')
menu_markup.add(update, on_off_system, update_subs, get_code_wp)
back_menu.add(back_menu_button)
delete_markup.add(delete_button)


def get_code_wp():
    url = f"https://{apiUrlInstance}/waInstance{idInstance}/getAuthorizationCode/{apiTokenInstance}"
    payload = {"phoneNumber": phoneNumber}
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        data = response.json()
        return data.get("code")
    else:
        print("Ошибка запроса:", response.text)
        return None


def get_wp_status():
    url = f"https://{apiUrlInstance}/waInstance{idInstance}/getWaSettings/{apiTokenInstance}"
    response = requests.request("GET", url)
    if response.status_code == 200:
        data = response.json()
        return True if data.get("stateInstance") == 'authorized' else False
    else:
        return None


def get_status_token_ats():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM settings")
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return "не активна"
    token = row[0]
    url = f"https://cloudpbx.beeline.ru/apis/portal/subscription?subscriptionId={token}"
    headers = {"X-MPBX-API-AUTH-TOKEN": ATS_TOKEN}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        expires_seconds = data.get("expires", 0)
        if expires_seconds <= 0:
            return "не активна"
        minutes = expires_seconds // 60
        hours = minutes // 60
        remaining_minutes = minutes % 60
        if hours > 0:
            return f"{hours} ч {remaining_minutes} мин"
        else:
            return f"{minutes} мин"
    else:
        return "не активна"


def get_status_analyze():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT status_analyze FROM settings")
    status = cursor.fetchone()[0]
    conn.close()
    return status


def subscribe_xsi_events():
    subscription_url = "https://cloudpbx.beeline.ru/apis/portal/subscription"
    callback_url = f"http://{HOST}/subscription"
    headers = {
        "X-MPBX-API-AUTH-TOKEN": ATS_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {
        "pattern": "201",
        "expires": 86400,
        "subscriptionType": "ADVANCED_CALL",
        "url": callback_url
    }
    response = requests.put(subscription_url, headers=headers, json=payload)
    if response.status_code == 200:
        subscription_result = response.json()
        print("Подписка успешно создана!")
        return subscription_result.get('subscriptionId')
    elif response.status_code == 400:
        return None
    else:
        print(f"⚠ Ошибка: {response.status_code} - {response.text}")
        return None


def get_status_system():
    text = f'''{'🟢Cтатус системы анализа звоноков: работает' if get_status_analyze() == 1 else '🔴Cтатус системы анализа звоноков: не работает'}
{'🟢Статус аккаунта WhatsApp: авторизован' if get_wp_status() else '🔴Статус аккаунта WhatsApp: не авторизован'}

⏳Статус подписки на звонки: {get_status_token_ats()}
    '''
    return text


def stop_subs():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM settings")
    row = cursor.fetchone()
    token = row[0]
    conn.close()
    url = f"https://cloudpbx.beeline.ru/apis/portal/subscription?subscriptionId={token}"
    headers = {"X-MPBX-API-AUTH-TOKEN": ATS_TOKEN}
    response = requests.delete(url, headers=headers)
    if response.status_code == 200:
        print(f"✅ Подписка {token} успешно удалена.")
        return True
    else:
        print(f"❌ Ошибка 400: Некорректный запрос при удалении подписки {token}.")
        return False


@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, text=
    '''🎮 *Помощник по бронированию в компьютерном клубе Lockers* 🎮
    
    *Как я работаю? 🤖*
    Я помогу тебе *отправлять и подготавливать* сообщения о бронировании для гостей клуба *быстро и удобно*! 🔥
    
    🔹 *1. Отправка бронирования через переписку*
    📌 *Что нужно сделать?*
    - Скопируй *всю переписку с гостем* в *одно сообщение* (не пересылай, а именно *скопируй и вставь*).
    - В переписке должны быть *все важные детали*: *дата, время, количество компьютеров и т. д.*
    - Отправь это *мне в одном сообщении* – и я сразу же подготовлю *готовый шаблон ответа* для гостя.
    
    📩 *Что ты получишь?*
    - Готовое сообщение (проверь его на корректность), которое можно *отправить гостю* в любом мессенджере.
    
    🔹 *2. Автоматическое сообщение после звонка 📞*
    📌 *Как это работает?*
    - После разговора с гостем *я сам анализирую ваш разговор* и автоматически создаю *сообщение с подтверждением брони*.
    - Тебе остается *только проверить текст*, и если всё верно – нажать *"Отправить"* ✅.
    - Если есть ошибки – нажми *"Корректировать"*, исправь текст и отправь.
    
    ⚡ *Преимущества:*
    ✔ Экономия времени ⏳
    ✔ Минимум ручной работы 📝
    ✔ Гости получают подтверждение брони быстро 🚀
    
    Если что-то непонятно – *я всегда на связи* (*@chadoyev_ru*) 🎯
    
    🔍Чтобы открыть меню введи /menu
    ''', parse_mode='Markdown')


@bot.message_handler(commands=['menu'])
def menu(message):
    if message.chat.id == ADMIN_ID:
        bot.send_message(message.chat.id,
                         text=get_status_system(), reply_markup=menu_markup)
    else:
        bot.reply_to(message, 'Бот предназначен не для вас.')


@bot.message_handler(func=lambda message: True)
def generateTextFromSMS(message):
    if message.chat.id == ADMIN_ID:
        booking_info = message.text.strip()
        msg = bot.send_message(message.chat.id, 'Подставляю всё в шаблон, жди.', parse_mode='Markdown')
        # Получаем сегодняшнюю дату в формате ГГГГ-ММ-ДД
        today = datetime.date.today().strftime('%Y-%m-%d')
        # Формируем промпт для GPT-4 с передачей сегодняшней даты
        prompt = f'''Текущая дата: {today}\n\n

    **Инструкция по интерпретации исходной информации и формирования ответа:**

    1. **Определения терминов:**
       - *Локер, зона, комната, зал* — все эти термины обозначают помещение, в котором просят забронировать ПК.

    2. **Типы помещений и их характеристики:**
       - **Премьер-локер:**
         - Имеет номер **1**.
         - Единственная комната с мониторами 390 Гц. Если в сообщении встречается упоминание «390 Гц», это указывает на Премьер-локер.
         - Количество ПК: 5
         - Относится к *кальянной зоне* (номера 1, 2, 3, 4 — кальянная).
       - **VIP-локеры:**
         - Имеют номера **2, 3, 4**.
         - Количество ПК в одной комнате: 5
         - Также относятся к кальянной зоне.
       - **Locker на 8 ПК:**
         - Имеет номер **5**.
         - Иногда называется «общий зал».
         - Количество ПК: 8
         - Относится к *некурящей зоне*.

    3. **Обработка запросов гостей:**
       - Если гость запрашивает определённый компьютер (например, «3 компьютер в премьере»), это означает, что он имеет в виду третий ПК в соответствующей комнате, а не 3 отдельных ПК.

    4. **Работа с отсутствующей информацией:**
       - Если какая-либо информация отсутствует в исходном сообщении (**booking_info**), оставьте соответствующее поле пустым или выберите наиболее подходящий вариант.

    5. **Учет относительных обозначений дат:**
       - Если гость указывает «завтра» или «послезавтра», используйте значение {today} для корректного вычисления даты.
       - Дата в итоговом сообщении должна быть в формате дд.мм.гггг.

    6. **Исходные данные:**
       {booking_info}

    ---

    **Шаблон итогового сообщения:**

    *Ваше бронирование в клубе Lockers подтверждено!*

    📍 *Забронированная зона:* [Кальянная / Некурящая]

    💻 *Комната:* [VIP-локер №2, №3, №4 / Premier-локер №1 / VIP-локер на 8 ПК]

    ⏰ *Время:* [дата] с [время начала] до [время окончания]

    👥 *Количество компьютеров:* [число ПК]

    ⚠️ *Ваша бронь будет действовать в назначенное время и последующие 15 минут.* Если у вас вдруг изменятся планы, пожалуйста, предупредите нас заранее. В ином случае, ваша бронь автоматически аннулируется.

    До встречи в *Lockers!* 🎮🔥

    ---

    **Задача:**  
    На основе информации из исходных данных сформируй сообщение, следуя данному шаблону, учитывая все вышеприведённые правила и условия.

        '''

        try:
            response = client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system",
                     "content": "Ты помощник, который генерирует сообщения бронирования по заданному шаблону."},
                    {"role": "user", "content": prompt}
                ]
            )

            result_text = response.choices[0].message.content
            bot.edit_message_text(chat_id=ADMIN_ID, message_id=msg.message_id,
                                  text=result_text,
                                  parse_mode='Markdown')


        except Exception as e:
            bot.reply_to(message, f"Ошибка при генерации сообщения: {e}")
    else:
        bot.reply_to(message, f"Бот предназначен не для вас.")


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if call.data == 'update':
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text='Обновляю...')
        time.sleep(1)
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=get_status_system(),
                              reply_markup=menu_markup)
    if call.data == 'get_code_wp':
        code = get_code_wp()
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id,
                              text=f'Введите этот код: {code} в WhatsApp', reply_markup=back_menu)
    if call.data == 'update_subs':
        current_datetime = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subs_id = subscribe_xsi_events()
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE settings SET token = ?, last_time_got_token = ?", (subs_id, current_datetime))
        conn.commit()
        conn.close()
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text='✅ Подписка на звонки обновлена', reply_markup=back_menu)
    if call.data == 'back_menu':
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=get_status_system(),
                              reply_markup=menu_markup)
    if call.data == 'on_off_analyze':
        status = get_status_analyze()
        if status == 1:
            stop_subs()
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("UPDATE settings SET status_analyze = 0")
            conn.commit()
            conn.close()
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=get_status_system(),
                                  reply_markup=menu_markup)
        else:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            new_token = subscribe_xsi_events()
            if new_token:
                current_time = datetime.datetime.now()
                new_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("UPDATE settings SET token = ?, last_time_got_token = ?", (new_token, new_time))
                conn.commit()
                print("✅ Подписка успешно обновлена.")
            cursor.execute("UPDATE settings SET status_analyze = 1")
            conn.commit()
            conn.close()
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.id, text=get_status_system(),
                                  reply_markup=menu_markup)
    # Обработка нажатий кнопок "Отправить" и "Корректировать"
    if call.data.startswith("send_"):
        ext_tracking_id = call.data.split("_", 1)[1]
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT phone_number, generated_message FROM recordings WHERE ext_tracking_id = ?",
                       (ext_tracking_id,))
        row = cursor.fetchone()
        if row:
            phone_num, generated_message = row
            if generated_message:
                if sentWP(phone_num, generated_message):
                    cursor.execute("UPDATE recordings SET sent_to_user = 1 WHERE ext_tracking_id = ?",
                                   (ext_tracking_id,))
                    conn.commit()
                    bot.answer_callback_query(call.id, "✅Сообщение успешно отправлено!")
                    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                          text="✅Сообщение отправлено.", parse_mode='Markdown')
                else:
                    bot.answer_callback_query(call.id, "Ошибка при отправке сообщения!")
            else:
                bot.answer_callback_query(call.id, "Сообщение не найдено.")
        else:
            bot.answer_callback_query(call.id, "Запись не найдена.")
        conn.close()
    if call.data.startswith("correct_"):
        ext_tracking_id = call.data.split("_")[1]
        phone = call.data.split("_")[2]
        msg = bot.send_message(call.message.chat.id, "Введите корректировки для сообщения:")
        bot.register_next_step_handler(msg, process_correction, ext_tracking_id, phone)
    if call.data == 'delete_msg':
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)

def process_correction(message, ext_tracking_id, phone):
    corrected_text = message.text
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE recordings SET generated_message = ? WHERE ext_tracking_id = ?",
                   (corrected_text, ext_tracking_id))
    conn.commit()
    conn.close()
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton('📨Отправить', callback_data=f"send_{ext_tracking_id}"),
        types.InlineKeyboardButton('✏Корректировать', callback_data=f"correct_{ext_tracking_id}_{phone}"), delete_button
    )
    bot.send_message(message.chat.id, f'Предлагаемое сообщение для {phone}:\n{corrected_text}', reply_markup=markup,
                     parse_mode='Markdown')


def sentWP(phoneNumber, message):
    url = f"https://{apiUrlInstance}/waInstance{idInstance}/sendMessage/{apiTokenInstance}"
    payload = {"chatId": f"{phoneNumber}@c.us", "message": f"{message}"}
    headers = {'Content-Type': 'application/json'}
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        return True
    else:
        return False


def extract_booking_status(response):
    lines = response.splitlines()
    if lines:
        first_line = lines[0].strip()
        if first_line.startswith("TRUE") or first_line.startswith("FALSE"):
            return first_line
    return None


def generateTextFromCall(booking_info, ext_tracking_id, phoneNumber):
    booking_info = booking_info.strip()
    today = datetime.date.today().strftime('%Y-%m-%d')
    prompt = f'''Текущая дата: {today}

**Инструкция по интерпретации транскрипции разговора с гостем и формированию ответа:**

*Начало ответа:*
- Первая строка должна начинаться с ключевого слова, определяющего характер запроса:
  - Если из транскрипции следует, что гость просит забронировать и администратор подтвердил бронь, вставьте в начале слово:
TRUE
  - Если же из транскрипции видно, что гость лишь интересуется информацией или разговор не имеет характера запроса на бронирование, начните ответ с:  
FALSE


**Далее следуйте инструкциям для формирования заполненного шаблона:**

1. **Общие положения:**
   - Исходная информация: **{booking_info}** представляет собой транскрипцию разговора с гостем, содержащую разговорные особенности, неформальный стиль, опечатки, сокращения и сленг.
   - Из этой транскрипции необходимо извлечь всю важную информацию для бронирования, а именно:
     - Забронированная зона (Кальянная / Некурящая)
     - Комната (VIP-локер №2, №3, №4 / Premier-локер №1 / Locker на 8 ПК)
     - Дата бронирования (формат дд.мм.гггг)
     - Время начала и окончания бронирования
     - Количество компьютеров для бронирования

2. **Определения терминов и особенности бронирования:**
   - *Локер, зона, комната, зал* — все эти термины обозначают помещение, где просят забронировать ПК.
   - **Премьер-локер:**
     - Имеет номер **1**.
     - Единственная комната с мониторами 390 Гц. Если в разговоре встречается упоминание «390 Гц», значит, речь идёт о Премьер-локере.
     - Количество ПК: 5
     - Относится к *кальянной зоне* (номера 1, 2, 3, 4 — кальянная).
   - **VIP-локеры:**
     - Имеют номера **2, 3, 4**.
     - Количество ПК в одной комнате: 5
     - Также относятся к кальянной зоне.
   - **VIP-локер на 8 ПК:**
     - Имеет номер **5**.
     - Иногда называется «общий зал».
     - Количество ПК: 8
     - Относится к *некурящей зоне*.

3. **Обработка разговорных особенностей:**
   - Учитывайте неформальные выражения, сокращения и возможные опечатки. Например, если гость говорит «3 комп в премьере», это означает, что он имеет в виду третий компьютер в Премьер-локере, а не заказ трёх ПК.
   - Извлекайте данные бронирования независимо от стилистических особенностей транскрипции.

4. **Работа с отсутствующей информацией:**
   - Если какая-либо из необходимых деталей отсутствует(или не совпадает с выбором из шаблона) в транскрипции, оставьте соответствующее поле пустым или выберите наиболее подходящий вариант.

5. **Учет относительных обозначений дат:**
   - Если гость указывает «завтра» или «послезавтра», используйте значение {today} для вычисления соответствующей даты.
   - Дата в итоговом сообщении должна быть в формате дд.мм.гггг.

---

**Шаблон итогового сообщения:**

*Ваше бронирование в клубе Lockers подтверждено!*

📍 *Забронированная зона:* [Кальянная / Некурящая]

💻 *Комната:* [VIP-локер №2, №3, №4 / Premier-локер №1 / VIP-локер на 8 ПК]

⏰ *Время:* [дата] с [время начала] до [время окончания]

👥 *Количество компьютеров:* [число ПК]

⚠️ *Ваша бронь будет действовать в назначенное время и последующие 15 минут.* Если у вас вдруг изменятся планы, пожалуйста, предупредите нас заранее. В ином случае, ваша бронь автоматически аннулируется.

До встречи в *Lockers!* 🎮🔥

---

**Задача:**  
На основе исходной информации(транскрипция разговора с гостем на русском языке), сформируйте итоговое сообщение, заполнив все поля в шаблоне согласно извлечённой информации. В первой строке добавьте ключевое слово, как описано выше, чтобы указать, является ли запрос бронированием (TRUE) или просто информацией (FALSE).


    '''
    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system",
                 "content": "Ты помощник, который генерирует сообщения бронирования по заданному шаблону."},
                {"role": "user", "content": prompt}
            ]
        )
        result_text = response.choices[0].message.content
        print(result_text)
        status = extract_booking_status(result_text)
        if status == 'FALSE':
            return
        elif status == "TRUE":
            result_text = result_text.replace(status, '')
        else:
            return
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE recordings SET generated_message = ? WHERE ext_tracking_id = ?",
                       (result_text, ext_tracking_id))
        conn.commit()
        conn.close()
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton('📨Отправить', callback_data=f"send_{ext_tracking_id}"),
            types.InlineKeyboardButton('✏Корректировать', callback_data=f"correct_{ext_tracking_id}_{phoneNumber}"), delete_button
        )
        bot.send_message(chat_id=ADMIN_ID,
                         text=f'Номер телефона: {phoneNumber}\nСообщение: {result_text}',
                         parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        print(f'Ошибка при генерации сообщения из разговора: {e}')


def attempt_download_recording(ext_tracking_id, max_attempts=10, interval=5):
    download_url = (f"https://cloudpbx.beeline.ru/apis/portal/v2/records/"
                    f"{ext_tracking_id}/{phone_ats}@ip.beeline.ru/download?"
                    f"extTrackingId={ext_tracking_id}&userId={phone_ats}%40ip.beeline.ru")
    headers = {"X-MPBX-API-AUTH-TOKEN": ATS_TOKEN}
    file_path = os.path.join(RECORDS_DIR, f"recording_{ext_tracking_id}.mp3")
    for attempt in range(1, max_attempts + 1):
        response = requests.get(download_url, headers=headers)
        if response.status_code == 200:
            with open(file_path, "wb") as file:
                file.write(response.content)
            print(f"[{attempt}/{max_attempts}] Запись звонка сохранена как {file_path}")
            return file_path
        else:
            print(f"[{attempt}/{max_attempts}] Ошибка при загрузке записи: {response.status_code}, {response.text}")
            if attempt < max_attempts:
                print(f"Повторная попытка через {interval} сек...")
                time.sleep(interval)
    print(f"Не удалось скачать запись после {max_attempts} попыток.")
    return None


def transcribe_recording(file_path):
    if not file_path:
        print("Файл для расшифровки не найден.")
        return None
    try:
        with open(file_path, "rb") as audio_file:
            transcript_data = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        transcript_text = transcript_data.text
        return transcript_text
    except Exception as e:
        print(f"Ошибка при расшифровке записи: {e}")
        return None


def transcription_worker():
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT id, ext_tracking_id, phone_number FROM recordings WHERE transcript = ''")
            records = cursor.fetchall()
            for rec_id, ext_tracking_id, phone_number in records:
                print(f"🔄 Обработка записи id={rec_id}, extTrackingId={ext_tracking_id}")
                file_path = attempt_download_recording(ext_tracking_id, max_attempts=15, interval=10)
                if file_path:
                    transcript_text = transcribe_recording(file_path)
                    if transcript_text:
                        cursor.execute("UPDATE recordings SET transcript = ? WHERE id = ?", (transcript_text, rec_id))
                        conn.commit()
                        print(f"✅ Запись id={rec_id} обновлена с транскрипцией.")
                        generateTextFromCall(transcript_text, ext_tracking_id, phone_number)
                    else:
                        print(f"❌ Не удалось расшифровать запись id={rec_id}.")
                        cursor.execute("UPDATE recordings SET transcript = ? WHERE id = ?", ('Не получилось.', rec_id))
                        conn.commit()
                else:
                    print(f"⚠ Не удалось скачать запись для id={rec_id}.")
                    cursor.execute("UPDATE recordings SET transcript = ? WHERE id = ?", ('Не получилось.', rec_id))
                    conn.commit()
            if get_status_analyze() == 1:
                cursor.execute("SELECT token, last_time_got_token FROM settings")
                row = cursor.fetchone()
                if row:
                    token, last_time_got_token = row
                    last_time = datetime.datetime.strptime(last_time_got_token, "%Y-%m-%d %H:%M:%S")
                    current_time = datetime.datetime.now()
                    time_elapsed = (current_time - last_time).total_seconds()
                    time_remaining = SUBSCRIPTION_LIFETIME - time_elapsed
                    if time_remaining <= RENEW_THRESHOLD:
                        print("🔄 Подписка скоро истекает, обновляем...")
                        new_token = subscribe_xsi_events()
                        if new_token:
                            new_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
                            cursor.execute("UPDATE settings SET token = ?, last_time_got_token = ?", (new_token, new_time))
                            conn.commit()
                            print("✅ Подписка успешно обновлена.")
                        else:
                            print("⚠ Ошибка при обновлении подписки.")
                conn.close()
            time.sleep(POLL_INTERVAL)
        except:
            time.sleep(5)


@app.route("/subscription", methods=["POST"])
def handle_event():
    data = request.data.decode("utf-8")
    try:
        root = ET.fromstring(data)
        event_data = root.find(".//{http://schema.broadsoft.com/xsi}eventData")
        event_type = event_data.attrib.get('{http://www.w3.org/2001/XMLSchema-instance}type', '')
        if event_type == "xsi:CallReleasedEvent":
            ext_tracking_id_elem = root.find(".//{http://schema.broadsoft.com/xsi}extTrackingId")
            tel_number_elem = root.find(
                ".//{http://schema.broadsoft.com/xsi}remoteParty/{http://schema.broadsoft.com/xsi}address")
            recorded = root.find(".//{http://schema.broadsoft.com/xsi}recorded")
            if ext_tracking_id_elem is not None and tel_number_elem is not None and recorded is not None:
                ext_tracking_id = ext_tracking_id_elem.text
                tel_number = tel_number_elem.text
                phone_number = ''.join(filter(str.isdigit, tel_number))
                print(f"Звонок завершен. extTrackingId: {ext_tracking_id}, tel: {phone_number}, запись имеется.")
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM recordings WHERE ext_tracking_id = ?", (ext_tracking_id,))
                existing = cursor.fetchone()
                if not existing:
                    cursor.execute(
                        "INSERT INTO recordings (phone_number, ext_tracking_id, transcript, sent_to_user) VALUES (?, ?, ?, ?)",
                        (phone_number, ext_tracking_id, "", 0)
                    )
                    conn.commit()
                    print(f"Новая запись добавлена для extTrackingId: {ext_tracking_id}")
                else:
                    print(f"Запись с extTrackingId: {ext_tracking_id} уже существует. Добавление не требуется.")
                conn.close()
    except Exception as e:
        print("Ошибка обработки XML:", str(e))
    return jsonify({"status": "received"}), 200


def run_flask():
    app.run(host='0.0.0.0', port=FLASK_PORT)


def run_bot():
    while True:
        try:
            bot.infinity_polling()
        except Exception as e:
            print(f"Ошибка бота: {e}")
            time.sleep(5)


if __name__ == "__main__":
    init_db()
    init_storage()
    transcription_thread = threading.Thread(target=transcription_worker, daemon=True)
    transcription_thread.start()
    telegram_bot = threading.Thread(target=run_bot, daemon=True)
    telegram_bot.start()
    run_flask()

