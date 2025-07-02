import logging
import asyncio
from io import BytesIO
from typing import Dict, List
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from qrcode import QRCode

# Configuration
configFile = open( 'config.txt' )
cnf = configFile.read().split( '@' )
BOT_TOKEN   = cnf[0]
API_ID      = cnf[1]       
API_HASH    = cnf[2]

# Key variables
monitored_channels: Dict[str, List[int]] = {  }  # { "channel_username": [user_id1, user_id2] }
user_sessions: Dict[int, str] = {  }  # { user_id: "telethon_session_string" }
active_qr_logins = {  }  # { user_id: (client, qr_login) }

# Logging configuration
logging.basicConfig(  
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level = logging.INFO
 )
logger = logging.getLogger(  __name__  )

# ===== Telegram Bot Handlers =====
async def start(  update: Update, context: ContextTypes.DEFAULT_TYPE  ) -> None:
    keyboard = [
        [KeyboardButton( "🔐 Авторизация" ), KeyboardButton( "➕ Добавить канал" )],
        [KeyboardButton( "📋 Мои каналы" ), KeyboardButton( "❌ Отписаться" )]
    ]
    reply_markup = ReplyKeyboardMarkup( keyboard, resize_keyboard = True )

    await update.message.reply_text( 
        "👋 Привет! Я бот для получения постов из каналов.\n\n"
        "1. Нажми '🔐 Авторизация' для входа через QR-код\n"
        "2. Добавь каналы через '➕ Добавить канал'\n"
        "3. Получай новые посты автоматически!",
        reply_markup = reply_markup
     )

async def button_handler(  update: Update, context: ContextTypes.DEFAULT_TYPE  ) -> None:
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "🔐 Авторизация":
        await generate_qr_login( update, user_id )
    elif text == "➕ Добавить канал":
        await handle_add_channel( update, user_id )
    elif text == "📋 Мои каналы":
        await my_channels( update, context )
    elif text == "❌ Отписаться":
        await handle_unsubscribe( update, user_id )

async def generate_qr_login(  update: Update, user_id: int  ):
    if user_id in active_qr_logins:
        await update.message.reply_text( "❌ У вас уже есть активный QR-код" )
        return
    
    client = TelegramClient( StringSession(  ), API_ID, API_HASH )
    await client.connect(  )
    qr_login = await client.qr_login(  )
    
    # Generate QR code
    qr = QRCode(  )
    qr.add_data( qr_login.url )
    img = qr.make_image( fill_color = "black", back_color = "white" )
    
    # Send QR code to user
    img_io = BytesIO(  )
    img.save( img_io, 'PNG' )
    img_io.seek( 0 )
    
    await update.message.reply_photo(
        photo = img_io,
        caption = "🔑 Отсканируйте этот QR-код в Telegram:\n"
                  "1. Откройте Telegram на телефоне\n"
                  "2. Настройки → Устройства → Подключить устройство\n"
                  "3. Сканируйте этот код\n\n"
                  "⏳ QR-код действителен 1 минуту"
    )
    
    active_qr_logins[user_id] = ( client, qr_login )
    asyncio.create_task( wait_for_login( update, user_id, client, qr_login ) )

async def wait_for_login(  update: Update, user_id: int, client: TelegramClient, qr_login ):
    try:
        await qr_login.wait(  timeout = 60 )
        
        if await qr_login.is_signed_in(  ):
            session_string = client.session.save(  )
            user_sessions[user_id] = session_string
            await update.message.reply_text( "✅ Авторизация прошла успешно!" )
            asyncio.create_task( start_telethon_client( user_id, session_string ) )
        else:
            await update.message.reply_text( "❌ Вход не выполнен. Попробуйте снова." )
    except Exception as e:
        logger.error( f"Ошибка QR-авторизации: { e }" )
        await update.message.reply_text( "⌛ Время действия QR-кода истекло" )
    finally:
        if user_id in active_qr_logins:
            del active_qr_logins[user_id]
        await client.disconnect(  )

async def handle_add_channel(  update: Update, user_id: int  ):
    if user_id not in user_sessions:
        await update.message.reply_text( "❌ Сначала авторизуйтесь через '🔐 Авторизация'" )
        return
    await update.message.reply_text( "📝 Введите @username канала для добавления:" )

async def handle_unsubscribe(  update: Update, user_id: int  ):
    if user_id not in user_sessions:
        await update.message.reply_text( "❌ Сначала авторизуйтесь через '🔐 Авторизация'" )
        return
    await update.message.reply_text( "📝 Введите @username канала для отписки:" )

async def handle_message(  update: Update, context: ContextTypes.DEFAULT_TYPE  ):
    text = update.message.text
    user_id = update.effective_user.id
    
    if text.startswith( "@" ):
        context.args = [text[1:]]  # Remove @ symbol
        if user_id in user_sessions:
            if "добав" in update.message.reply_to_message.text.lower(  ):
                await add_channel( update, context )
            elif "отпис" in update.message.reply_to_message.text.lower(  ):
                await unsubscribe( update, context )
        else:
            await update.message.reply_text( "❌ Сначала авторизуйтесь через '🔐 Авторизация'" )

async def add_channel( update: Update, context: ContextTypes.DEFAULT_TYPE ) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text( "Введите username канала (например, channel_name)" )
        return
    
    channel_username = context.args[0]
    if channel_username not in monitored_channels:
        monitored_channels[channel_username] = []
    
    if user_id not in monitored_channels[channel_username]:
        monitored_channels[channel_username].append( user_id )
        await update.message.reply_text( f"✅ Теперь вы будете получать посты из @{ channel_username }!" )
    else:
        await update.message.reply_text( "ℹ️ Вы уже подписаны на этот канал" )

async def my_channels( update: Update, context: ContextTypes.DEFAULT_TYPE ) -> None:
    user_id = update.effective_user.id
    user_channels = [
        f"@{ channel }" for channel, users in monitored_channels.items(  )
        if user_id in users
    ]
    
    if not user_channels:
        await update.message.reply_text( "У вас нет активных подписок" )
    else:
        await update.message.reply_text( 
            "📢 Ваши каналы:\n" + "\n".join( user_channels )
         )

async def unsubscribe( update: Update, context: ContextTypes.DEFAULT_TYPE ) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text( "Введите username канала от которого хотите отписаться:" )
        return
    
    channel_username = context.args[0]
    if channel_username in monitored_channels and user_id in monitored_channels[channel_username]:
        monitored_channels[channel_username].remove( user_id )
        await update.message.reply_text( f"❌ Вы отписались от @{ channel_username }" )
    else:
        await update.message.reply_text( "ℹ️ Вы не подписаны на этот канал" )

# ===== Telethon Monitor =====
async def start_telethon_client( user_id: int, session_string: str ):
    application = Application.builder().token( BOT_TOKEN ).build()
    bot = application.bot
    
    async with TelegramClient( 
        StringSession( session_string ), API_ID, API_HASH
     ) as client:
        @client.on( events.NewMessage )
        async def handler( event ):
            try:
                chat = await event.get_chat(  )
                if not chat.username:
                    return
                
                channel_username = chat.username
                if channel_username in monitored_channels:
                    for user in monitored_channels[channel_username]:
                        if user == user_id:
                            await bot.send_message( 
                                chat_id = user,
                                text = f"📢 Новый пост из @{ channel_username }:\n\n{ event.text }"
                             )
            except Exception as e:
                logger.error( f"Ошибка: { e }" )
        
        await client.run_until_disconnected()

# ===== Launch =====
def main():
    application = Application.builder().token( BOT_TOKEN ).build()
    
    # Handlers
    application.add_handler( CommandHandler( "start", start ) )
    application.add_handler( MessageHandler( filters.TEXT & ~filters.COMMAND, button_handler ) )
    application.add_handler( MessageHandler( filters.TEXT & ~filters.COMMAND, handle_message ) )
    
    application.run_polling()

if __name__ == '__main__':
    main()