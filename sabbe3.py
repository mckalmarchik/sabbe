#файл admin.py
from aiogram import types

import kb
from bot import dp, bot
from handlers.fsm import *
from handlers.db import db_profile_access, db_profile_exist, db_profile_updateone, db_profile_exist_usr, db_profile_get_usrname
from configurebot import cfg

errormessage = cfg['error_message']
lvl1name = cfg['1lvl_adm_name']
lvl2name = cfg['2lvl_adm_name']
lvl3name = cfg['3lvl_adm_name']
devid = cfg['dev_id']

def extract_arg(arg):
    return arg.split()[1:]

async def admin_ot(message: types.Message):
    try:
        uid = message.from_user.id

        if(db_profile_access(uid) >= 1):
            args = extract_arg(message.text)
            if len(args) >= 2:
                chatid = str(args[0])
                args.pop(0)
                answer = ""
                for ot in args:
                    answer+=ot+" "
                await message.reply('✅ Вы успешно ответили на вопрос!')
                await bot.send_message(chatid, f"✉ Новое уведомление!\n\n`{answer}`",parse_mode='Markdown')
                return
            else:
                await message.reply('⚠ Укажите аргументы команды\nПример: `/ответ 516712732 Ваш ответ`',parse_mode='Markdown')
                return
        else:
            return
    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')

async def admin_giveaccess(message: types.Message):
    try:
        uidown = message.from_user.id

        if (db_profile_access(uidown) >= 3):
            args = extract_arg(message.text)
            if len(args) == 2:
                uid = int(args[0])
                access = int(args[1])
                outmsg = ""      
                if db_profile_exist(uid):
                    if access == 0:
                        outmsg = "✅ Вы успешно сняли все доступы с этого человека!"
                    elif access == 1:
                        outmsg = f"✅ Вы успешно выдали доступ *{lvl1name}* данному человеку!"
                    elif access == 2:
                        outmsg = f"✅ Вы успешно выдали доступ *{lvl2name}* данному человеку!"
                    elif access == 3:
                        outmsg = f"✅ Вы успешно выдали доступ *{lvl3name}* данному человеку!"
                    else:
                        await message.reply('⚠ Максимальный уровень доступа: *3*', parse_mode='Markdown')
                        return
                    db_profile_updateone({'_id': uid}, {"$set": {"access": access}})
                    await message.reply(outmsg, parse_mode='Markdown')
                    return
                else:
                    await message.reply("⚠ Этого пользователя *не* существует!",parse_mode='Markdown')
                    return
            else:
                await message.reply('⚠ Укажите аргументы команды\nПример: `/доступ 516712372 1`',
                                    parse_mode='Markdown')
                return

        else:
            return
    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')

async def admin_ban(message: types.Message):
    try:
        uidown = message.from_user.id

        if db_profile_access(uidown) >= 2:
            args = extract_arg(message.text)
            if len(args) == 2:
                uid = int(args[0])
                reason = args[1]
                if db_profile_exist(uid):
                    db_profile_updateone({"_id": uid}, {"$set": {'ban': 1}})
                    await message.reply(f'✅ Вы успешно забанили этого пользователя\nПричина: `{reason}`',parse_mode='Markdown')
                    await bot.send_message(uid, f"⚠ Администратор *заблокировал* Вас в боте\nПричина: `{reason}`", parse_mode='Markdown')
                    return
                else:
                    await message.reply("⚠ Этого пользователя *не* существует!", parse_mode='Markdown')
                    return
            else:
                await message.reply('⚠ Укажите аргументы команды\nПример: `/бан 51623722 Причина`',
                                    parse_mode='Markdown')
                return
    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')

async def admin_unban(message: types.Message):
    try:
        uidown = message.from_user.id

        if db_profile_access(uidown) >= 2:
            args = extract_arg(message.text)
            if len(args) == 1:
                uid = int(args[0])
                if db_profile_exist(uid):
                    db_profile_updateone({"_id": uid}, {"$set": {'ban': 0}})
                    await message.reply(f'✅ Вы успешно разблокировали этого пользователя',parse_mode='Markdown')
                    await bot.send_message(uid, f"⚠ Администратор *разблокировал* Вас в боте!", parse_mode='Markdown')
                    return
                else:
                    await message.reply("⚠ Этого пользователя *не* существует!", parse_mode='Markdown')
                    return
            else:
                await message.reply('⚠ Укажите аргументы команды\nПример: `/разбан 516272834`',
                                    parse_mode='Markdown')
                return
    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')

async def admin_id(message: types.Message):
    try:
        args = extract_arg(message.text)
        if len(args) == 1:
            username = args[0]
            if db_profile_exist_usr(username):
                uid = db_profile_get_usrname(username, '_id')
                await message.reply(f"🆔 {uid}")
            else:
                await message.reply("⚠ Этого пользователя *не* существует!", parse_mode='Markdown')
                return
        else:
            await message.reply('⚠ Укажите аргументы команды\nПример: `/айди nosemka`',
                                parse_mode='Markdown')
            return
    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')

def register_handler_admin():
    dp.register_message_handler(admin_ot, commands=['ответ', 'ot'])
    dp.register_message_handler(admin_giveaccess, commands=['доступ', 'access'])
    dp.register_message_handler(admin_ban, commands=['бан', 'ban'])
    dp.register_message_handler(admin_unban, commands=['разбан', 'unban'])
    dp.register_message_handler(admin_id, commands=['айди', 'id'])

#файл clien.py
from aiogram import types

import kb
from bot import dp, bot
from handlers.fsm import *
from handlers.db import db_profile_exist, db_profile_insertone, db_profile_banned
from configurebot import cfg


welcomemessage = cfg['welcome_message']
errormessage = cfg['error_message']
devid = cfg['dev_id']
aboutus = cfg['about_us']
question_first_msg = cfg['question_type_ur_question_message']

handler_button_new_question = cfg['button_new_question']
# handler_button_about_us = cfg['button_about_us']


async def client_start(message: types.Message):
    try:
        if(message.chat.type != 'private'):
            await message.answer('Данную команду можно использовать только в личных сообщениях с ботом.')
            return
        if db_profile_exist(message.from_user.id):
            await message.answer(f'{welcomemessage}',parse_mode='Markdown', reply_markup=kb.mainmenu)
        else:
            db_profile_insertone({
                '_id': message.from_user.id,
                'username': message.from_user.username,
                'access': 0,
                'ban': 0
            })
            print('Новый пользователь!')
            await message.answer(f'{welcomemessage}',parse_mode='Markdown', reply_markup=kb.mainmenu)
    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')

async def client_newquestion(message: types.Message):
    try:
        if message.text == handler_button_new_question:
            if db_profile_banned(message.from_user.id):
                await message.answer("⚠ Ви *заблоковані* у боті!", parse_mode='Markdown')
                return
            await message.answer(f"{question_first_msg}")
            await FSMQuestion.text.set()
        # elif message.text == handler_button_about_us:
        #     if db_profile_banned(message.from_user.id):
        #         await message.answer("⚠ Ви *заблоковані* у боті!", parse_mode='Markdown')
        #         return
        #     await message.answer(f"{aboutus}", disable_web_page_preview=True, parse_mode='Markdown')

    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')


async def client_getgroupid(message: types.Message):
    try:
        await message.answer(f"Chat id is: *{message.chat.id}*\nYour id is: *{message.from_user.id}*", parse_mode='Markdown')
    except Exception as e:
        cid = message.chat.id
        await message.answer(f"{errormessage}",
                             parse_mode='Markdown')
        await bot.send_message(devid, f"Случилась *ошибка* в чате *{cid}*\nСтатус ошибки: `{e}`",
                               parse_mode='Markdown')

def register_handler_client():
    dp.register_message_handler(client_start, commands='start', state=None)
    dp.register_message_handler(client_getgroupid, commands='getchatid')
    dp.register_message_handler(client_newquestion)

#файл fsm.py
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from bot import bot,dp
from configurebot import cfg

tehchatid = cfg['teh_chat_id']
message_seneded = cfg['question_ur_question_sended_message']

class FSMQuestion(StatesGroup):
	text = State()

# Обработчики
async def newquestion(message: types.Message, state: FSMContext):
	async with state.proxy() as data:
		if (message.content_type == 'photo'):
			data['text'] = message.caption
		else:
			data['text'] = message.text
	await state.finish()
	if(message.chat.username == None):
		who = "Ник не установлен"
	else:
		who = "@"+message.chat.username
	question = data['text']
	if(message.content_type=='photo'):
		ph = message.photo[0].file_id
		await message.reply(f"{message_seneded}",
							parse_mode='Markdown')
		await bot.send_photo(tehchatid, ph, caption=f"✉ | Новый вопрос\nОт: {who}\nВопрос: `{data['text']}`\n\n📝 Чтобы ответить на вопрос введите `/ответ {message.chat.id} Ваш ответ`",parse_mode='Markdown')
	else:
		await message.reply(f"{message_seneded}",
							parse_mode='Markdown')
		await bot.send_message(tehchatid,
							   f"✉ | Новый вопрос\nОт: {who}\nВопрос: `{data['text']}`\n\n📝 Чтобы ответить на вопрос введите `/ответ {message.chat.id} Ваш ответ`",
							   parse_mode='Markdown')

def register_handler_FSM():
	dp.register_message_handler(newquestion,state=FSMQuestion.text, content_types=['photo', 'text'])

