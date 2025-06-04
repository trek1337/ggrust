import os
import random
import aiosqlite
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
import json
import aiohttp

# Constants
FREE_READING = 1
READING_PRICE = 299
HELP_TEXT = (
    "<b>Доступные команды:</b>\n"
    "/read &lt;вопрос&gt; — получить предсказание\n"
    "/balance — проверить баланс\n"
    "/topup &lt;сумма&gt; — пополнить баланс\n"
    "/profile — сведения о профиле\n"
    "/history — история предсказаний\n"
    "/menu — открыть меню\n"
    "/stats — статистика (для админа)"
)

# Conversation states for profile setup
ASK_NAME, ASK_AGE, ASK_GENDER = range(3)

# Menu callback identifiers
MENU_READ = 'menu_read'
MENU_BALANCE = 'menu_balance'
MENU_PROFILE = 'menu_profile'
MENU_HISTORY = 'menu_history'
MENU_HELP = 'menu_help'

# Tarot deck data loaded from cards.json
CARDS_PATH = os.path.join(os.path.dirname(__file__), 'cards.json')
with open(CARDS_PATH, 'r', encoding='utf-8') as f:
    TAROT_CARDS = json.load(f)

DB_PATH = 'tarot_bot.db'


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            '''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                age INTEGER,
                gender TEXT,
                balance INTEGER DEFAULT 0,
                free_used INTEGER DEFAULT 0
            )'''
        )
        await db.execute(
            '''CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                question TEXT,
                cards TEXT,
                answer TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )'''
        )
        await db.commit()


async def get_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, username, first_name, age, gender, balance, free_used FROM users WHERE user_id=?', (user_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            await db.execute('INSERT INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
            await db.commit()
            user = (user_id, username, None, None, None, 0, 0)
        else:
            user = row
        return user


async def update_profile(user_id: int, first_name: str, age: int, gender: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET first_name=?, age=?, gender=? WHERE user_id=?',
                         (first_name, age, gender, user_id))
        await db.commit()


async def update_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (amount, user_id))
        await db.commit()


async def set_free_used(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET free_used=1 WHERE user_id=?', (user_id,))
        await db.commit()


async def add_history(user_id: int, question: str, cards: list, answer: str):
    async with aiosqlite.connect(DB_PATH) as db:
        card_names = [c['name'] if isinstance(c, dict) else c for c in cards]
        await db.execute('INSERT INTO history (user_id, question, cards, answer) VALUES (?, ?, ?, ?)',
                         (user_id, question, ','.join(card_names), answer))
        await db.commit()


async def get_history(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT question, cards, answer, timestamp FROM history WHERE user_id=? ORDER BY timestamp DESC', (user_id,)) as cur:
            rows = await cur.fetchall()
        return rows


async def generate_answer(cards: list, question: str, profile: tuple) -> str:
    token = os.environ.get('PROXYAPI_TOKEN')
    if not token:
        # Fallback message if no API token set
        return (
            f"В ответ на ваш вопрос '{question}' карты {cards[0]['name']}, {cards[1]['name']} и {cards[2]['name']} "
            "подсказывают, что вы на перепутье. Доверьтесь интуиции и двигайтесь вперёд."
        )

    messages = [
        {
            'role': 'system',
            'content': 'Ты опытный таролог и отвечаешь коротко и по делу.'
        },
        {
            'role': 'user',
            'content': (
                f"Имя: {profile[2]}\nВозраст: {profile[3]}\nПол: {profile[4]}\n"
                f"Вопрос: {question}\n"
                f"Карты: {cards[0]['name']} - {cards[0]['description']}; "
                f"{cards[1]['name']} - {cards[1]['description']}; "
                f"{cards[2]['name']} - {cards[2]['description']}"
            )
        },
    ]
    payload = {'model': 'gpt-3.5-turbo', 'messages': messages}
    headers = {'Authorization': f'Bearer {token}'}
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.proxyapi.ru/openai/v1/chat/completions', json=payload, headers=headers) as resp:
            if resp.status != 200:
                return 'Не удалось получить ответ ИИ.'
            data = await resp.json()
            return data['choices'][0]['message']['content']


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await get_user(user.id, user.username or '')
    if data[2] is None:
        await update.message.reply_text('Как вас зовут?')
        return ASK_NAME
    text = (
        f"Здравствуйте, <b>{data[2]}</b>! Добро пожаловать в бота гаданий на Таро.\n"
        f"Первое гадание бесплатно. Стоимость последующих — {READING_PRICE} руб.\n"
        "Для списка команд введите /help."
    )
    await update.message.reply_text(text, parse_mode='HTML')


async def name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['first_name'] = update.message.text.strip()
    await update.message.reply_text('Сколько вам лет?')
    return ASK_AGE


async def age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.isdigit():
        await update.message.reply_text('Пожалуйста, введите число. Сколько вам лет?')
        return ASK_AGE
    context.user_data['age'] = int(update.message.text)
    buttons = [[InlineKeyboardButton('Мужской', callback_data='M'), InlineKeyboardButton('Женский', callback_data='F')]]
    await update.message.reply_text('Ваш пол?', reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_GENDER


async def gender_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gender = update.callback_query.data
    await update.callback_query.answer()
    context.user_data['gender'] = gender
    user = update.effective_user
    await update_profile(user.id, context.user_data['first_name'], context.user_data['age'], gender)
    text = (
        f"Спасибо, {context.user_data['first_name']}! Профиль сохранён.\n"
        f"Первое гадание бесплатно. Стоимость последующих — {READING_PRICE} руб.\n"
        "Для списка команд введите /help."
    )
    await update.callback_query.message.reply_text(text, parse_mode='HTML')
    return ConversationHandler.END


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton('Гадать', callback_data=MENU_READ)],
        [InlineKeyboardButton('Баланс', callback_data=MENU_BALANCE)],
        [InlineKeyboardButton('Профиль', callback_data=MENU_PROFILE)],
        [InlineKeyboardButton('История', callback_data=MENU_HISTORY)],
        [InlineKeyboardButton('Помощь', callback_data=MENU_HELP)],
    ]
    await update.message.reply_text(
        'Выберите действие:',
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == MENU_READ:
        await query.message.reply_text('Введите команду /read <вопрос>')
    elif query.data == MENU_BALANCE:
        await balance(update, context)
    elif query.data == MENU_PROFILE:
        await profile(update, context)
    elif query.data == MENU_HISTORY:
        await history(update, context)
    elif query.data == MENU_HELP:
        await help_command(update, context)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await get_user(user.id, user.username or '')
    balance = data[5]
    free_used = data[6]
    name = data[2] or 'не указано'
    age = data[3] or 'не указан'
    gender = {'M':'Мужской','F':'Женский'}.get(data[4],'не указан')
    text = (
        f"<b>Профиль {user.username}</b>\n"
        f"Имя: {name}\n"
        f"Возраст: {age}\n"
        f"Пол: {gender}\n"
        f"Баланс: {balance} руб.\n"
        f"Бесплатное гадание использовано: {'да' if free_used else 'нет'}"
    )
    await update.message.reply_text(text, parse_mode='HTML')


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = await get_history(user.id)
    if not rows:
        await update.message.reply_text('История пуста.', parse_mode='HTML')
        return
    messages = []
    for q, cards, answer, ts in rows:
        messages.append(f"<b>{ts}</b> — {q}\nКарты: {cards}\n{answer}")
    await update.message.reply_text('\n\n'.join(messages), parse_mode='HTML')


async def read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if not args:
        await update.message.reply_text('Укажите вопрос после команды /read <ваш вопрос>', parse_mode='HTML')
        return
    question = ' '.join(args)
    user_data = await get_user(user.id, user.username or '')
    balance = user_data[5]
    free_used = user_data[6]

    if free_used >= FREE_READING and balance < READING_PRICE:
        await update.message.reply_text('Недостаточно средств. Пополните баланс командой /topup <сумма>', parse_mode='HTML')
        return

    cards = random.sample(TAROT_CARDS, 3)
    answer = await generate_answer(cards, question, user_data)

    if free_used < FREE_READING:
        await set_free_used(user.id)
    else:
        await update_balance(user.id, -READING_PRICE)

    await add_history(user.id, question, cards, answer)

    card_text = ', '.join(c['name'] for c in cards)
    for card in cards:
        if card['image']:
            await update.message.reply_photo(card['image'], caption=card['name'])
    message = f"<b>Карты:</b> {card_text}\n<pre>{answer}</pre>"
    await update.message.reply_text(message, parse_mode='HTML')


async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text('Используйте /topup <сумма>', parse_mode='HTML')
        return
    amount = int(args[0])
    await update_balance(user.id, amount)
    await update.message.reply_text(f'Баланс пополнен на {amount} руб.', parse_mode='HTML')


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await get_user(user.id, user.username or '')
    await update.message.reply_text(f'Ваш баланс: {data[5]} руб.', parse_mode='HTML')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode='HTML')


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.environ.get('ADMIN_USER_ID')
    if str(update.effective_user.id) != str(admin_id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT COUNT(*) FROM users') as cur:
            user_count = (await cur.fetchone())[0]
        async with db.execute('SELECT SUM(balance) FROM users') as cur:
            balance_sum = (await cur.fetchone())[0] or 0
    await update.message.reply_text(
        f'Пользователей: {user_count}\nСуммарный баланс: {balance_sum} руб.',
        parse_mode='HTML'
    )


def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        print('Set TELEGRAM_BOT_TOKEN env variable')
        return

    import asyncio
    asyncio.run(init_db())

    app = ApplicationBuilder().token(token).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_handler)],
            ASK_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_handler)],
            ASK_GENDER: [CallbackQueryHandler(gender_handler)],
        },
        fallbacks=[],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('menu', menu))
    app.add_handler(CommandHandler('profile', profile))
    app.add_handler(CommandHandler('history', history))
    app.add_handler(CommandHandler('read', read))
    app.add_handler(CommandHandler('topup', topup))
    app.add_handler(CommandHandler('balance', balance))
    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CallbackQueryHandler(menu_action, pattern='^menu_'))

    print('Bot started...')
    app.run_polling()


if __name__ == '__main__':
    main()
