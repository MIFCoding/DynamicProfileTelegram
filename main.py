import os
import asyncio
import logging
import io
import re
from datetime import datetime, timezone, timedelta, time
from typing import Optional, Tuple, Callable, Dict, Any, Awaitable
from collections import defaultdict

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import BaseMiddleware
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.photos import UploadProfilePhotoRequest, DeletePhotosRequest
from telethon.errors import FloodWaitError
from telethon.tl.types import InputPhoto
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

BOT_TOKEN = os.getenv('BOT_API_KEY')
USER_ID = int(os.getenv('USER_ID'))
OPENWEATHER_API_KEY = os.getenv('OPENWEATHERMAP_API_KEY')
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_NAME = 'profile_changer'
FIXED_INTERVAL = 5  # Фиксированный интервал 5 минут

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

logging.getLogger('asyncio').setLevel(logging.WARNING)
logging.getLogger('aiohttp').setLevel(logging.WARNING)

DAY_START = time(6, 0)
DAY_END = time(21, 0)

IMGS = ["TEMPLATE", "CLOCK", "HOUR_HAND", "MINUTE_HAND",
        "SUN", "MOON", "SNOW", "CLOUD", "RAIN"]

FONT_PATH = "fonts/calibri.ttf"

CITY_BOX = (172, 285, 1108, 615)
C_BOX = (1080, 706, 1180, 796)
TIME_BOX = (250, 670, 625, 865)
TEMP_BOX = (835, 670, 1095, 865)

I = {img: Image.open(f"images/{img.lower()}.png") for img in IMGS}


class SharedData:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.city_name: Optional[str] = None
        self.profile_text: Optional[str] = None
        self.lat: Optional[float] = None
        self.lon: Optional[float] = None
        self.last_flood_wait: Optional[float] = None
        self.flood_wait_until: Optional[datetime] = None
        self.running = True
        self.last_update_time: Optional[datetime] = None

    def is_running(self) -> bool:
        return self.running

    async def update(self, city_name: str, profile_text: str, lat: float, lon: float):
        async with self.lock:
            self.city_name = city_name
            self.profile_text = profile_text
            self.lat = lat
            self.lon = lon

    async def get(self) -> Tuple[Optional[str], Optional[str], Optional[float], Optional[float]]:
        async with self.lock:
            return (self.city_name, self.profile_text, self.lat, self.lon)

    async def set_flood_wait(self, seconds: float):
        async with self.lock:
            self.last_flood_wait = seconds
            self.flood_wait_until = datetime.now() + timedelta(seconds=seconds)

    async def get_flood_info(self) -> Tuple[Optional[float], Optional[datetime]]:
        async with self.lock:
            return (self.last_flood_wait, self.flood_wait_until)

    async def stop(self):
        async with self.lock:
            self.running = False

    async def update_last_time(self):
        async with self.lock:
            self.last_update_time = datetime.now()

    async def get_last_time(self) -> Optional[datetime]:
        async with self.lock:
            return self.last_update_time


shared_data = SharedData()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class MessageStore:
    def __init__(self):
        self.chat_messages = defaultdict(list)

    def add_message(self, chat_id: int, message_id: int):
        self.chat_messages[chat_id].append(message_id)

    def get_messages(self, chat_id: int) -> list[int]:
        return self.chat_messages.get(chat_id, [])

    def clear_chat(self, chat_id: int):
        if chat_id in self.chat_messages:
            del self.chat_messages[chat_id]


message_store = MessageStore()


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.Message | types.CallbackQuery, Dict[str, Any]], Awaitable[Any]],
        event: types.Message | types.CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, types.Message):
            user_id = event.from_user.id
        elif isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id
        else:
            return await handler(event, data)

        if user_id != USER_ID:
            if isinstance(event, types.Message):
                await event.answer("❌ Извините, этот бот доступен только для авторизованных пользователей.")
            elif isinstance(event, types.CallbackQuery):
                await event.answer("❌ Доступ запрещён.", show_alert=True)
            return
        return await handler(event, data)


class CleanupMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        if not event.text and not event.caption and not event.document and not event.photo:
            return await handler(event, data)

        if event.from_user.id == bot.id:
            return await handler(event, data)
        
        state: FSMContext | None = data.get('state')
        current_state = await state.get_state() if state else None

        if event.text and event.text.startswith('/'):
            if state:
                await state.clear()
                logger.info(f"Состояние FSM сброшено для команды: {event.text}")

            message_ids = message_store.get_messages(event.chat.id)
            for msg_id in message_ids:
                try:
                    await bot.delete_message(event.chat.id, msg_id)
                except Exception as e:
                    logger.error(f"Ошибка удаления сообщения {msg_id}: {e}")
            message_store.clear_chat(event.chat.id)

            try:
                await event.delete()
            except Exception as e:
                logger.error(f"Ошибка удаления команды: {e}")

            return await handler(event, data)

        if current_state:
            message_store.add_message(event.chat.id, event.message_id)
            return await handler(event, data)

        try:
            await event.delete()
        except Exception as e:
            logger.error(f"Ошибка удаления сообщения: {e}")


dp.message.outer_middleware(AccessMiddleware())
dp.message.outer_middleware(CleanupMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


class CitySelection(StatesGroup):
    choosing_city = State()
    waiting_for_text = State()


def translate_weather(weather_id: int, current_time: time) -> str:
    group = weather_id // 100

    if group in [2, 3, 5]:
        return "RAIN"
    elif group == 6:
        return "SNOW"
    elif group == 7:
        return "CLOUD"
    elif group == 8:
        if (weather_id % 10) in [0, 1]:
            return "SUN" if DAY_START <= current_time < DAY_END else "MOON"
        return "CLOUD"


async def get_city_coordinates(session: aiohttp.ClientSession, city_name: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {'q': city_name, 'countrycodes': 'ru', 'format': 'json'}
    try:
        async with session.get(url, params=params, timeout=10) as response:
            response.raise_for_status()
            return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def get_weather_data(session: aiohttp.ClientSession, lat: float, lon: float):
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
    try:
        async with session.get(url, timeout=10) as response:
            response.raise_for_status()
            return await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


@dp.message(Command("set"))
async def cmd_set(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        logger.warning(f"Обнаружено активное состояние {current_state} при запуске /set")
    
    msg = await message.answer("📍 Введите название населенного пункта в России:")
    message_store.add_message(message.chat.id, msg.message_id)
    await state.set_state(CitySelection.choosing_city)

@dp.message(CitySelection.choosing_city, F.text)
async def process_city_name(message: types.Message, state: FSMContext):
    city_name = message.text.strip()
    async with aiohttp.ClientSession() as session:
        cities = await get_city_coordinates(session, city_name)
        if not cities:
            msg = await message.answer("❌ Населенный пункт не найден. Попробуйте еще раз:")
            message_store.add_message(message.chat.id, msg.message_id)
            return
        await state.update_data(cities=cities, current_index=0)
        await show_city_pagination(message, state)


async def show_city_pagination(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cities = data['cities']
    current_index = data.get('current_index', 0)
    city = cities[current_index]

    text = f"<b>Тип:</b> {city.get('type', 'неизвестный тип').capitalize()}\n"
    text += f"<b>Название:</b> {city.get('display_name', 'Неизвестно')}\n\n"

    builder = InlineKeyboardBuilder()

    if len(cities) > 1:
        builder.row(
            InlineKeyboardButton(text="⬅️", callback_data="prev_city"),
            InlineKeyboardButton(
                text=f"{current_index+1}/{len(cities)}", callback_data="current"),
            InlineKeyboardButton(text="➡️", callback_data="next_city")
        )
        builder.row(InlineKeyboardButton(
            text="✅ Выбрать", callback_data="select_city"))
    else:
        builder.row(InlineKeyboardButton(
            text="✅ Выбрать", callback_data="select_city"))

    if 'pagination_message_id' in data:
        try:
            msg = await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=data['pagination_message_id'],
                text=text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            message_store.add_message(message.chat.id, msg.message_id)
        except Exception:
            msg = await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            message_store.add_message(message.chat.id, msg.message_id)
            await state.update_data(pagination_message_id=msg.message_id)
    else:
        msg = await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        message_store.add_message(message.chat.id, msg.message_id)
        await state.update_data(pagination_message_id=msg.message_id)


@dp.callback_query(F.data.in_(["prev_city", "next_city", "select_city"]))
async def handle_pagination(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cities = data['cities']
    current_index = data.get('current_index', 0)

    if callback.data == "prev_city":
        current_index = (current_index - 1) % len(cities)
        await state.update_data(current_index=current_index)
        await show_city_pagination(callback.message, state)
    elif callback.data == "next_city":
        current_index = (current_index + 1) % len(cities)
        await state.update_data(current_index=current_index)
        await show_city_pagination(callback.message, state)
    elif callback.data == "select_city":
        await callback.message.delete()
        message_store.add_message(
            callback.message.chat.id, callback.message.message_id)
        await process_selected_city(cities[current_index], callback.message, state)
        return

    await callback.answer()


async def process_selected_city(city, message: types.Message, state: FSMContext):
    await state.update_data(selected_city=city)
    msg = await message.answer("✒️ Какую надпись поставить в профиль?")
    message_store.add_message(message.chat.id, msg.message_id)
    await state.set_state(CitySelection.waiting_for_text)


@dp.message(CitySelection.waiting_for_text, F.text)
async def process_profile_text(message: types.Message, state: FSMContext):
    profile_text = message.text.strip()
    data = await state.get_data()
    city = data['selected_city']
    lat = float(city['lat'])
    lon = float(city['lon'])
    city_name = city['display_name']

    await shared_data.update(city_name, profile_text, lat, lon)
    msg = await message.answer("💾 Надпись принята! Данные сохранены.")
    message_store.add_message(message.chat.id, msg.message_id)
    await state.clear()


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да", callback_data="confirm_stop"),
        InlineKeyboardButton(text="❌ Нет", callback_data="cancel_stop")
    )
    msg = await message.answer(
        "Вы уверены, что хотите отключить бота?",
        reply_markup=builder.as_markup()
    )
    message_store.add_message(message.chat.id, msg.message_id)


@dp.callback_query(F.data == "confirm_stop")
async def confirm_stop(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    try:
        msg = await callback.message.edit_text(
            f"⏹ Бот остановлен.",
            reply_markup=None
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        await callback.answer(f"⏹ Бот остановлен.", show_alert=True)
    else:
        await callback.answer()

    await asyncio.sleep(5)
    try:
        await msg.delete()
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщения: {e}")

    await shared_data.stop()
    await dp.stop_polling()


@dp.callback_query(F.data == "cancel_stop")
async def cancel_stop(callback: types.CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)

    try:
        await callback.message.edit_text(
            f"✅ Продолжаю работу...",
            reply_markup=None
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        await callback.answer(f"✅ Продолжаю работу...", show_alert=True)
    else:
        await callback.answer()


@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    city_name, profile_text, _, _ = await shared_data.get()
    last_update = await shared_data.get_last_time()
    _, flood_until = await shared_data.get_flood_info()

    last_update_text = "еще не обновлялся"
    if last_update:
        last_update_text = last_update.strftime("%H:%M:%S")

    flood_status = "🟢 нет ограничений"
    if flood_until and datetime.now() < flood_until:
        remaining = int((flood_until - datetime.now()).total_seconds())

        hours = remaining // 3600
        hours = f'{hours:02d}' if hours < 10 else hours
        remaining_seconds = remaining % 3600
        minutes = remaining_seconds // 60
        seconds = remaining_seconds % 60

        flood_status = f"🔴 <b>flood-бан</b>, осталось: <b>{hours}:{minutes:02d}:{seconds:02d}</b>"

    info_text = (
        "<b>📊 Статус бота</b>\n\n"
        f"<b>Последнее обновление:</b> {last_update_text}\n"
        f"<b>Ограничения:</b> {flood_status}\n"
        f"<b>Текущая надпись:</b> {profile_text or 'не установлена'}\n"
        f"<b>Населенный пункт:</b> {city_name or 'не установлен'}"
    )

    msg = await message.answer(info_text, parse_mode="HTML")
    message_store.add_message(message.chat.id, msg.message_id)


def place_overlay_on_base(base: Image, overlay: Image, x: int, y: int):
    position = (x - overlay.width // 2, y - overlay.height // 2)
    base.paste(overlay, position, overlay)


def draw_scaled_text(image: Image, text: str, box: tuple, font_path: str,
                     color: tuple = (255, 255, 255), size_fonts: int = -1):
    left, top, right, bottom = box
    area_width = right - left
    area_height = bottom - top
    draw = ImageDraw.Draw(image)

    words = text.split()

    def split_text(font):
        lines = []
        current_line = []
        current_width = 0
        space_width = draw.textlength(" ", font=font)

        for word in words:
            word_width = draw.textlength(word, font=font)

            if word_width > area_width and not current_line:
                parts = []
                part = ""
                for char in word:
                    char_width = draw.textlength(char, font=font)
                    if current_width + char_width <= area_width:
                        part += char
                        current_width += char_width
                    else:
                        if part:
                            parts.append(part)
                        part = char
                        current_width = char_width
                if part:
                    parts.append(part)

                for part in parts:
                    lines.append(part)
                continue

            if not current_line or current_width + space_width + word_width <= area_width:
                current_line.append(word)
                current_width += word_width + \
                    (space_width if current_line else 0)
            else:
                lines.append(" ".join(current_line))
                current_line = [word]
                current_width = word_width

        if current_line:
            lines.append(" ".join(current_line))

        return lines

    def calculate_height(lines, font):
        if not lines:
            return 0
        bbox = draw.textbbox((0, 0), "Hg", font=font)
        line_height = bbox[3] - bbox[1]
        return len(lines) * line_height * 1.1

    if size_fonts != -1:
        best_font = ImageFont.truetype(font_path, size_fonts)
        best_lines = split_text(best_font)

    else:
        min_font_size = 10
        max_font_size = 277
        best_font = None
        best_lines = None

        while min_font_size <= max_font_size:
            mid_font_size = (min_font_size + max_font_size) // 2
            try:
                current_font = ImageFont.truetype(font_path, mid_font_size)
            except:
                current_font = ImageFont.truetype(font_path, 10)

            lines = split_text(current_font)
            text_height = calculate_height(lines, current_font)

            max_line_width = 0
            for line in lines:
                line_width = draw.textlength(line, font=current_font)
                if line_width > max_line_width:
                    max_line_width = line_width

            if text_height <= area_height and max_line_width <= area_width:
                best_font = current_font
                best_lines = lines
                min_font_size = mid_font_size + 1
            else:
                max_font_size = mid_font_size - 1

        if best_font is None:
            best_font = ImageFont.truetype(font_path, min_font_size)
            best_lines = split_text(best_font)

    bbox = draw.textbbox((0, 0), "Hg", font=best_font)
    line_height = bbox[3] - bbox[1]
    spacing = line_height * 0.1

    total_height = len(best_lines) * line_height + \
        (len(best_lines) - 1) * spacing
    y = top + (area_height - total_height) // 2

    for line in best_lines:
        line_width = draw.textlength(line, font=best_font)
        x = left + (area_width - line_width) // 2
        draw.text((x, y), line, font=best_font, fill=color)
        y += line_height + spacing


def draw_clock(clock: Image, hour_img: Image, minute_img: Image, time_str: str):
    center_x, center_y = clock.width // 2, clock.height // 2

    hours, minutes = map(int, time_str.split(":"))

    hour_angle = -((hours % 12) * 30 + minutes * 0.5)
    minute_angle = -(minutes * 6)

    def rotate_hand(img, angle, anchor_y, offset=0):
        expanded_size = int(max(img.size) * 3)
        temp_img = Image.new(
            "RGBA", (expanded_size, expanded_size), (0, 0, 0, 0))

        hand_x = expanded_size // 2 - img.width // 2

        hand_y = expanded_size // 2 - (anchor_y - offset)
        temp_img.paste(img, (hand_x, hand_y))

        rotated = temp_img.rotate(angle, resample=Image.BICUBIC, expand=False)

        new_center_x = rotated.width // 2
        new_center_y = rotated.height // 2
        return rotated, new_center_x, new_center_y

    offset_pixels = 5

    hour_rot, h_cx, h_cy = rotate_hand(
        hour_img, hour_angle, hour_img.height, offset_pixels)
    minute_rot, m_cx, m_cy = rotate_hand(
        minute_img, minute_angle, minute_img.height, offset_pixels)

    clock.alpha_composite(hour_rot, dest=(center_x - h_cx, center_y - h_cy))
    clock.alpha_composite(minute_rot, dest=(center_x - m_cx, center_y - m_cy))


def generate_icon(city: str, time_str: str, temp: str, weather: str) -> Image:
    base = I["TEMPLATE"].copy()
    clock = I["CLOCK"].copy()

    draw_scaled_text(base, city, CITY_BOX, FONT_PATH)
    draw_scaled_text(base, "°C", C_BOX, FONT_PATH)
    draw_scaled_text(base, time_str, TIME_BOX, FONT_PATH, size_fonts=160)
    draw_scaled_text(base, temp, TEMP_BOX, FONT_PATH, size_fonts=160)

    draw_clock(clock, I["HOUR_HAND"], I["MINUTE_HAND"], time_str)
    place_overlay_on_base(base, clock, 170, 772)
    place_overlay_on_base(base, I[weather], 755, 772)

    return base


def round_to_nearest_5_minutes(dt: datetime) -> datetime:
    minute = dt.minute
    rounded_minute = (minute // 5) * 5
    if minute % 5 >= 3:
        rounded_minute += 5
    if rounded_minute >= 60:
        rounded_minute = 0
        dt += timedelta(hours=1)
    return dt.replace(minute=rounded_minute, second=0, microsecond=0)


async def run_telethon():
    client = TelegramClient(SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()
    logger.info("Автосмена аватара запущена")
    to_delete = []

    while shared_data.is_running():
        try:
            now = datetime.now()
            rounded_time = round_to_nearest_5_minutes(now)
            last_update_time = await shared_data.get_last_time()

            update_needed = False
            if last_update_time is None:
                update_needed = True
            else:
                # Проверяем, нужно ли обновление (если текущее округленное время больше последнего обновления)
                if rounded_time > round_to_nearest_5_minutes(last_update_time):
                    update_needed = True

            if update_needed:
                city_name, profile_text, lat, lon = await shared_data.get()

                if None in (city_name, profile_text, lat, lon):
                    logger.info("Данные для аватара не установлены. Пропуск.")
                else:
                    if len(to_delete) == 10:
                        await client(DeletePhotosRequest(id=to_delete))
                        to_delete.clear()
                        logger.info("Очистка галереи профиля")

                    async with aiohttp.ClientSession() as session:
                        weather_data = await get_weather_data(session, lat, lon)
                        if weather_data:
                            tz_offset = timedelta(seconds=weather_data['timezone'])
                            local_time = datetime.now(timezone(tz_offset))
                            rounded_local_time = round_to_nearest_5_minutes(local_time)
                            formatted_time = rounded_local_time.strftime("%H:%M")
                            
                            temp = int(weather_data['main']['temp'])
                            weather_id = weather_data['weather'][0]['id']
                            weather_cond = translate_weather(
                                weather_id,
                                datetime.now().time()
                            )

                            if temp > 0:
                                temp = f"+{temp}"
                            elif temp < 0:
                                temp = f"{temp}"
                            else:
                                temp = "0"

                            icon = generate_icon(
                                re.sub(r"[ -]{2,}", " ", profile_text),
                                formatted_time,
                                temp,
                                weather_cond
                            )

                            with io.BytesIO() as buffer:
                                icon.save(buffer, format='PNG')
                                buffer.seek(0)

                                result = await client(UploadProfilePhotoRequest(file=await client.upload_file(buffer, file_name="icon.png")))

                                to_delete.append(InputPhoto(id=result.photo.id, access_hash=result.photo.access_hash,
                                                            file_reference=result.photo.file_reference))
                            logger.info(f"Аватар успешно обновлен (время: {formatted_time})")
                            await shared_data.update_last_time() 
                        else:
                            logger.warning("Не удалось получить данные о погоде")

            await asyncio.sleep(10)

        except FloodWaitError as e:
            wait_seconds = e.seconds
            logger.warning(f"Ожидание {wait_seconds} секунд из-за ограничений Telegram")
            await shared_data.set_flood_wait(wait_seconds)
            await asyncio.sleep(wait_seconds + 1)
        except Exception as e:
            logger.error(f"Ошибка в Telethon: {e}", exc_info=True)
            await asyncio.sleep(10)

    await client.disconnect()
    logger.info("Telethon клиент остановлен")


async def run_bot():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main():
    bot_task = asyncio.create_task(run_bot())
    telethon_task = asyncio.create_task(run_telethon())

    await asyncio.gather(bot_task, telethon_task)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")