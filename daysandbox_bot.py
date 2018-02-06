#!/usr/bin/env python
from pprint import pprint
import re
from collections import Counter
import jsondate
import json
import logging
from argparse import ArgumentParser
from datetime import datetime, timedelta
import html
import time
from traceback import format_exc

from telegram import ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from telegram.error import TelegramError
from telegram.ext.dispatcher import run_async

from model import load_joined_users, load_group_config
from util import find_username_links, find_external_links, fetch_user_type
from database import connect_db

HELP = """*DaySandBox Bot Help*

This bot implements simple anti-spam technique - it deletes all posts which:
1. contains link or @username or forwarded from somewhere
2. AND posted by the user who has joined the group less than 24 hours ago

Bot processes only @username links related to group/channel, if @username link points to other user it is not filtered by bot.

This bot does not ban anybody, it only deletes messages by the rules listed above. The idea is that in these 24 hours the spamer would be banned anyway for posting spam to other groups that are not protected by [@daysandbox_bot](https://t.me/daysandbox_bot).

*Usage*

1. Add [@daysandbox_bot](https://t.me/daysandbox_bot) to your group.
2. Go to group settings / users list / promote user to admin
3. Enable only one item: Delete messages
4. Click SAVE button
5. Enjoy!

*Commands*

`/help` - display this help message
`/stat` - display simple statistics about number of deleted messages
`/daysandbox_set publog=[yes|no]` - enable/disable messages to group about deleted posts
`/daysandbox_set safe_hours=[int]` - number in hours, how long new users are restricted to post links and forward posts, default is 24 hours (1 day). Allowed value is number between 1 and 8760 (365 days).
`/daysandbox_get publog` - get value of `publog` setting
`/daysandbox_get safe_hours` - get value of `safe_hours` setting

*How to log deleted messages to private channel*
Add bot to the channel as admin. Write `/setlog` to the channel. Forward message to the group.

Write /unsetlog in the group to disable logging to channel.

You can control format of logs with `/setlogformat <format>` command sent to the channel. The argument of this command could be: simple, json, forward or any combination of items delimited by space e.g. "json,forward":

- "simple" - display basic info about message + the
text of message (or caption text of photo/video)
- "json" - display full message data in JSON format
- "forward" - simply forward message to the channel (just message, no data about chat or author).

*Questions, Feedback*

Support group: [@tgrambots](https://t.me/tgrambots)

*Open Source*

The source code is available at [github.com/lorien/daysandbox_bot](https://github.com/lorien/daysandbox_bot)

*My Other Project*

[@nosticker_bot](https://t.me/nosticker_bot) - bot to delete stickers posted to group
[@coinsignal_robot](https://t.me/coinsignal_robot) - bot to be notified when price of specific coin reaches the level you have set, also you can use this bot just to see price of coins.
[@joinhider_bot](https://t.me/joinhider_bot) - bot to delete messages about user joined or left the chat

*Donation*
Ethereum: 0x00D0c93B180452a7c7B70F463aD9D24d9C6d4d61
Litecoin: LKJ86NwUkoboZyFHQwKPx8X984g3m3MPjC
Dash: XtGpsphiR2n9Shx9JFAwnuwGmWzSEvmrtU
UFO coin: CAdfaUR3tqfumoN7vQMVZ98CakyywgwK1L

"""
SUPERUSER_IDS = set([
    46284539, # @madspectator
])
# List of keys allowed to use in set_setting/get_setting
GROUP_SETTING_KEYS = ('publog', 'log_channel_id', 'logformat', 'safe_hours')
# Channel of global channel to translate ALL spam
GLOBAL_CHANNEL_ID = -1001148916224 
# Default time to reject link and forwarded posts from new user
DEFAULT_SAFE_HOURS = 24
db = connect_db()

# Some shitty global code
start = time.time()
JOINED_USERS = load_joined_users(db)
print('load_joined_users: %.2f' % (time.time() - start));
GROUP_CONFIG = load_group_config(db)
DELETE_EVENTS = {}


def save_event(db, event_type, msg, **kwargs):
    event = msg.to_dict()
    event.update({
        'date': datetime.utcnow(),
        'type': event_type,
    })
    event.update(**kwargs)
    db.event.save(event)


def set_setting(db, group_config, group_id, key, val):
    assert key in GROUP_SETTING_KEYS
    db.config.find_one_and_update(
        {
            'group_id': group_id,
            'key': key,
        },
        {'$set': {'value': val}},
        upsert=True,
    )
    group_config[(group_id, key)] = val


def get_setting(group_config, group_id, key, default=None):
    assert key in GROUP_SETTING_KEYS
    try:
        return group_config[(group_id, key)]
    except KeyError:
        return default


def process_user_type(db, username):
    username = username.lower()
    logging.debug('Querying %s type from db' % username)
    user = db.user.find_one({'username': username})
    if user:
        logging.debug('Record found, type is: %s' % user['type'])
        return user['type']
    else:
        logging.debug('Doing network request for type of %s' % username)
        user_type = fetch_user_type(username)
        logging.debug('Result is: %s' % user_type)
        if user_type:
            db.user.find_one_and_update(
                {'username': username},
                {'$set': {
                    'username': username,
                    'type': user_type,
                    'added': datetime.utcnow(),
                }},
                upsert=True
            )
        return user_type


@run_async
def handle_new_chat_members(bot, update):
    msg = update.message
    for user in msg.new_chat_members:
        now = datetime.utcnow()
        JOINED_USERS[(msg.chat.id, user.id)] = now
        db.joined_user.find_one_and_update(
            {
                'chat_id': msg.chat.id,
                'user_id': user.id,
                'chat_username': msg.chat.username,
                'user_username': user.username,
            },
            {'$set': {'date': now}},
            upsert=True,
        )

@run_async
def handle_start_help(bot, update):
    msg = update.effective_message
    if msg.chat.type == 'private':
        bot.send_message(
            msg.chat.id,
            HELP,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    else:
        if msg.text.strip() in (
                '/start', '/start@daysandbox_bot', '/start@daysandbox_test_bot',
                '/help', '/help@daysandbox_bot', '/help@daysandbox_test_bot'
            ):
            try:
                bot.delete_message(msg.chat.id, msg.message_id)
            except Exception as ex:
                if (
                        'message to delete not found' in str(ex)
                        #or "message can\'t be deleted" in str(ex)
                        or "be deleted" in str(ex) # quick fix
                        or 'MESSAGE_ID_INVALID' in str(ex)
                    ):
                    logging.error('Failed to delete command message: %s' % ex)
                else:
                    raise

@run_async
def handle_stat(bot, update):
    msg = update.effective_message
    if msg.chat.type != 'private':
        return
    days = []
    top_today = Counter()
    top_ystd = Counter()
    top_week = Counter()
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for x in range(7):
        day = today - timedelta(days=x)
        query = {'$and': [
            {'type': 'delete_msg'},
            {'date': {'$gte': day}},
            {'date': {'$lt': day + timedelta(days=1)}},
        ]}
        num = 0
        for event in db.event.find(query):
            num += 1
            if isinstance(event.get('chat'), dict):
                key  = (
                    '@%s' % event['chat']['username'] if event['chat'].get('username')
                    else '#%d' % event['chat']['id']
                )
            else:
                # OLD event format
                key  = (
                    '@%s' % event['chat_username'] if event['chat_username']
                    else '#%d' % event['chat_id']
                )
            if day == today:
                top_today[key] += 1
            if day == (today - timedelta(days=1)):
                top_ystd[key] += 1
            top_week[key] += 1
        days.insert(0, num)
    today_count = len(top_today)
    ystd_count = len(top_ystd)
    ret = 'Recent 7 days: %s' % ' | '.join([str(x) for x in days])
    ret += '\n\nTop today: (%s)\n%s' % (
        today_count,
        '\n'.join('  %s (%d)' % x for x in top_today.most_common(15))
    )
    ret += '\n\nTop yesterday: (%s)\n%s' % (
        ystd_count,
        '\n'.join('  %s (%d)' % x for x in top_ystd.most_common(15))
    )
    ret += '\n\nTop 10 week:\n%s' % '\n'.join('  %s (%d)' % x for x in top_week.most_common(10))
    bot.send_message(msg.chat.id, ret)

@run_async
def handle_set_get(bot, update):
    msg = update.effective_message
    if not msg.chat.type in ('group', 'supergroup'):
        bot.send_message(msg.chat.id, 'This command have to be called from the group')
        return
    re_cmd_set = re.compile(r'^/daysandbox_set (publog|safe_hours)=(.+)$')
    re_cmd_get = re.compile(r'^/daysandbox_get (publog|safe_hours)()$')
    if msg.text.startswith('/daysandbox_set'):
        match = re_cmd_set.match(msg.text)
        action = 'SET'
    else:
        match = re_cmd_get.match(msg.text)
        action = 'GET'
    if not match:
        bot.send_message(msg.chat.id, 'Invalid arguments') 
        return

    key, val = match.groups()

    admins = bot.get_chat_administrators(msg.chat.id)
    admin_ids = set([x.user.id for x in admins]) | set(SUPERUSER_IDS)
    if msg.from_user.id not in admin_ids:
        bot.send_message(msg.chat.id, 'Access denied')
        return

    if action == 'GET':
        bot.send_message(msg.chat.id, str(get_setting(GROUP_CONFIG, msg.chat.id, key)))
    else:
        if key == 'publog':
            if val in ('yes', 'no'):
                val_bool = (val == 'yes')
                set_setting(db, GROUP_CONFIG, msg.chat.id, key, val_bool)
                bot.send_message(msg.chat.id, 'Set public_notification to %s for group %s' % (
                    val_bool,
                    '@%s' % msg.chat.username if msg.chat.username else '#%d' % msg.chat.id,
                ))
            else:
                bot.send_message(msg.chat.id, 'Invalid public_notification value. Should be: yes or no')
        elif key == 'safe_hours':
            if not val.isdigit():
                bot.send_message(msg.chat.id, 'Invalid safe_hours value. Should be a number')
            val_int = int(val)
            max_hours = 24 * 365
            if val_int < 0 or val_int > max_hours:
                bot.send_message(msg.chat.id, 'Invalid safe_hours value. Should be a number between 1 and %d' % max_hours)
            set_setting(db, GROUP_CONFIG, msg.chat.id, key, val_int)
            bot.send_message(
                msg.chat.id,
                'Set safe_hours to %s for group %s' % (
                    val_int,
                    '@%s' % msg.chat.username if msg.chat.username else '#%d' % msg.chat.id,
                )
            )
        else:
            bot.send_message(msg.chat.id, 'Unknown action: %s' % key)

@run_async
def handle_setlogformat(bot, update):
    msg = update.effective_message
    # Possible options:
    # /setlogformat [json|forward]*
    if not msg.chat.type == 'channel':
        bot.send_message(msg.chat.id, 'This command have to be called from the channel')
        return
    #channel_admin_ids = [x.user.id for x in bot.get_chat_administrators(msg.chat.id)]
    #if msg.from_user.id not in channel_admin_ids:
    #    bot.send_message(msg.chat.id, 'Access denied')
    #    return
    valid_formats = ('json', 'forward', 'simple')
    formats = msg.text.split(' ')[-1].split(',')
    if any(x not in valid_formats for x in formats):
        bot.send_message(msg.chat.id, 'Invalid arguments. Valid choices: %s' % (', '.join(valid_formats),))
        return
    set_setting(db, GROUP_CONFIG, msg.chat.id, 'logformat', formats)
    bot.send_message(msg.chat.id, 'Set logformat for this channel')


@run_async
def handle_setlog(bot, update):
    msg = update.effective_message
    if not msg.chat.type in ('group', 'supergroup'):
        bot.send_message(msg.chat.id, 'This command have to be called from the group')
        return
    if not msg.forward_from_chat or msg.forward_from_chat.type != 'channel':
        bot.send_message(msg.chat.id, 'Command /setlog must be forwarded from channel')
        return
    channel = msg.forward_from_chat

    channel_admin_ids = [x.user.id for x in bot.get_chat_administrators(channel.id)]
    if bot.get_me().id not in channel_admin_ids:
        bot.send_message(msg.chat.id, 'I need to be an admin in log channel')
        return

    admins = bot.get_chat_administrators(msg.chat.id)
    admin_ids = set([x.user.id for x in admins]) | set(SUPERUSER_IDS)
    if msg.from_user.id not in admin_ids:
        bot.send_message(msg.chat.id, 'Access denied')
        return

    set_setting(db, GROUP_CONFIG, msg.chat.id, 'log_channel_id', channel.id)
    tgid = '@%s' % msg.chat.username if msg.chat.username else '#%d' % msg.chat.id
    bot.send_message(msg.chat.id, 'Set log channel for group %s' % tgid)

@run_async
def handle_unsetlog(bot, upate):
    msg = update.effective_message
    if not msg.chat.type in ('group', 'supergroup'):
        bot.send_message(msg.chat.id, 'This command have to be called from the group')
        return

    admins = bot.get_chat_administrators(msg.chat.id)
    admin_ids = set([x.user.id for x in admins]) | set(SUPERUSER_IDS)
    if msg.from_user.id not in admin_ids:
        bot.send_message(msg.chat.id, 'Access denied')
        return

    set_setting(db, GROUP_CONFIG, msg.chat.id, 'log_channel_id', None)
    tgid = '@%s' % msg.chat.username if msg.chat.username else '#%d' % msg.chat.id
    bot.send_message(msg.chat.id, 'Unset log channel for group %s' % tgid)


@run_async
def handle_any_message(bot, update):
    msg = update.effective_message
    if msg.chat.type == 'channel':
        return
    to_delete = False
    if msg.from_user.username == 'madspectator' and (msg.text == 'del' or msg.caption == 'del'):
        reason = 'debug delete'
        to_delete = True
    if not to_delete:
        try:
            join_date = JOINED_USERS[(msg.chat.id, msg.from_user.id)]
        except KeyError:
            return
        safe_hours = get_setting(GROUP_CONFIG, msg.chat.id, 'safe_hours', DEFAULT_SAFE_HOURS)
        if datetime.utcnow() - timedelta(hours=safe_hours) > join_date:
            return
    for ent in (msg.entities or []):
        if ent.type in ('url', 'text_link'):
            to_delete = True
            reason = 'external link'
            break
        if ent.type in ('email',):
            to_delete = True
            reason = 'email'
            break
        if ent.type == 'mention':
            username = msg.text[ent.offset:ent.offset + ent.length].lstrip('@')
            user_type = process_user_type(db, username)
            if user_type in ('group', 'channel'):
                to_delete = True
                reason = '@-link to group/channel'
                break
    if not to_delete:
        if msg.forward_from or msg.forward_from_chat:
            reason = 'forwarded'
            to_delete = True
    if not to_delete:
        usernames = find_username_links(msg.caption or '')
        for username in usernames:
            username = username.lstrip('@')
            user_type = process_user_type(db, username)
            if user_type in ('group', 'channel'):
                reason = 'caption @-link to group/channel'
                to_delete = True
                break
    if not to_delete:
        if find_external_links(msg.caption or ''):
            reason = 'caption external link'
            to_delete = True
    if to_delete:
        try:
            save_event(db, 'delete_msg', msg, reason=reason)
            if msg.from_user.first_name and msg.from_user.last_name:
                from_user = '%s %s' % (
                    msg.from_user.first_name,
                    msg.from_user.last_name,
                )
            elif msg.from_user.first_name:
                from_user = msg.from_user.first_name
            elif msg.from_user.username:
                from_user = msg.from_user.first_name
            else:
                from_user = '#%d' % msg.from_user.id
            event_key = (msg.chat.id, msg.from_user.id)
            if get_setting(GROUP_CONFIG, msg.chat.id, 'publog', True):
                # Notify about spam from same user only one time per hour
                if (
                        event_key not in DELETE_EVENTS
                        or DELETE_EVENTS[event_key] < datetime.utcnow() - timedelta(hours=1)
                    ):
                    ret = 'Removed msg from %s. Reason: new user + %s' % (from_user, reason)
                    bot.send_message(msg.chat.id, ret, parse_mode='HTML')
            DELETE_EVENTS[event_key] = datetime.utcnow()

            ids = set([GLOBAL_CHANNEL_ID])
            channel_id = get_setting(GROUP_CONFIG, msg.chat.id, 'log_channel_id')
            if channel_id:
                ids.add(channel_id)
            for chid in ids:
                formats = get_setting(GROUP_CONFIG, chid, 'logformat', default=['simple'])
                from_chatname = (
                    '@%s' % msg.chat.username if msg.chat.username
                    else '#%d' % msg.chat.id
                )
                if msg.from_user.username:
                    from_username = '@%s [%s]' % (
                        msg.from_user.username,
                        msg.from_user.first_name
                    )
                else:
                    from_username = msg.from_user.first_name
                from_info = (
                    'Chat: %s\nUser: <a href="tg://user?id=%d">%s</a>'
                    % (from_chatname, msg.from_user.id, from_username)
                )
                try:
                    if 'forward' in formats:
                        try:
                            bot.forward_message(chid, msg.chat.id, msg.message_id)
                        except Exception as ex:
                            db.fail.save({
                                'date': datetime.utcnow(),
                                'reason': str(ex),
                                'traceback': format_exc(),
                                'chat_id': msg.chat.id,
                                'msg_id': msg.message_id,
                            })
                            if (
                                    'MESSAGE_ID_INVALID' in str(ex)
                                    or 'message to forward not found' in str(ex)
                                ):
                                logging.error('Failed to process spam message: %s' % ex)
                            else:
                                raise
                    if 'json' in formats:
                        msg_dump = msg.to_dict()
                        msg_dump['meta'] = {
                            'reason': reason,
                            'date': datetime.utcnow(),
                        }
                        dump = jsondate.dumps(msg_dump, indent=4, ensure_ascii=False)
                        dump = html.escape(dump)
                        content = (
                            '%s\n<pre>%s</pre>' % (from_info, dump),
                        )
                        try:
                            bot.send_message(chid, content, parse_mode='HTML')
                        except Exception as ex:
                            if 'message is too long' in str(ex):
                                logging.error('Failed to log message to channel: %s' % ex)
                            else:
                                raise
                    if 'simple' in formats:
                        text = html.escape(msg.text or msg.caption)
                        content = (
                            '%s\nReason: %s\nContent:\n<pre>%s</pre>'
                            % (from_info, reason, text)
                        )
                        bot.send_message(chid, content, parse_mode='HTML')
                except Exception as ex:
                    logging.error(
                        'Failed to send notification to channel [%d]' % chid,
                        exc_info=ex
                    )
        finally:
            try:
                bot.delete_message(msg.chat.id, msg.message_id)
            except Exception as ex:
                db.fail.save({
                    'date': datetime.utcnow(),
                    'reason': str(ex),
                    'traceback': format_exc(),
                    'chat_id': msg.chat.id,
                    'msg_id': msg.message_id,
                })
                if (
                        'message to delete not found' in str(ex)
                        #or "message can\'t be deleted" in str(ex)
                        or "be deleted" in str(ex)
                        or 'MESSAGE_ID_INVALID' in str(ex)
                        or 'message to forward not found' in str(ex)
                    ):
                    logging.error('Failed to process spam message: %s' % ex)
                else:
                    raise


def init_bot_with_mode(mode):
    with open('var/config.json') as inp:
        config = json.load(inp)
    if mode == 'test':
        token = config['test_api_token']
    else:
        token = config['api_token']
    return Updater(token=token, workers=16)


def register_handlers(dispatcher):
    dispatcher.add_handler(MessageHandler(Filters.status_update.new_chat_members, handle_new_chat_members))
    dispatcher.add_handler(CommandHandler(['start', 'help'], handle_start_help))
    dispatcher.add_handler(CommandHandler('stat', handle_stat))
    dispatcher.add_handler(CommandHandler(['daysandbox_set', 'daysandbox_get'], handle_set_get))
    dispatcher.add_handler(CommandHandler('setlogformat', handle_setlogformat))
    dispatcher.add_handler(CommandHandler('setlog', handle_setlog))
    dispatcher.add_handler(CommandHandler('unsetlog', handle_unsetlog))
    dispatcher.add_handler(MessageHandler(
        (Filters.text | Filters.photo | Filters.video | Filters.audio | Filters.sticker | Filters.document),
        handle_any_message,
        edited_updates=True
    ))


def main():
    parser = ArgumentParser()
    parser.add_argument('--mode')
    opts = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG)
    updater = init_bot_with_mode(opts.mode)
    dispatcher = updater.dispatcher
    register_handlers(dispatcher)
    # TODO: remove web hook if any
    updater.start_polling()


if __name__ == '__main__':
    main()
