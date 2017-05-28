import argparse
import hashlib
import itertools
import os
import re
import shlex
import subprocess
import time

from sqlalchemy import Column, String, Integer, BigInteger, ForeignKey, Float
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker, relationship

from telegram import InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ChosenInlineResultHandler, InlineQueryHandler, Updater

import logging
logging.basicConfig(level=logging.DEBUG)

TG_TOKEN = os.environ["TG_TOKEN"]

engine = create_engine(os.environ["DB_URL"], echo=True)

db_session = scoped_session(
    sessionmaker(autocommit=False, autoflush=False, bind=engine))

Base = declarative_base()
Base.query = db_session.query_property()


def hash(x):
    m = hashlib.sha256()
    m.update(bytes(x, encoding="utf-8"))
    return m.hexdigest()


class Poll(Base):
    __tablename__ = 'polls'
    id = Column(BigInteger, primary_key=True)
    title = Column(String)
    creator_id = Column(BigInteger)
    options = relationship("Option")


class Option(Base):
    __tablename__ = 'options'
    id = Column(String, primary_key=True)
    title = Column(String)
    poll_id = Column(BigInteger, ForeignKey('polls.id'))
    poll = relationship("Poll", back_populates="options")


class Vote(Base):
    __tablename__ = 'votes'
    id = Column(Float, primary_key=True)
    user_id = Column(BigInteger, primary_key=True)
    option_id = Column(String, primary_key=True)


Base.metadata.create_all(bind=engine)


def generate_line(part, vote, total):
    if vote == 0:
        return "{}\n▫️  0%".format(part)
    return "{} - {}\n{} {:.0%}".format(part, vote,
                                       round(vote / total * 15) * "👍",
                                       vote / total)


def generate_message(question, parts, votes=None):
    if votes == None:
        votes = list(itertools.repeat(0, len(parts)))
    total = sum(votes)

    options = "\n\n".join(
        generate_line(part, vote, total) for part, vote in zip(parts, votes))
    return "*{}*\n\n{}".format(question, options)

def generate_button(query_id, part, vote):
    if vote == 0:
        return InlineKeyboardButton(
                text=part, callback_data=str(query_id) + hash(part)[:32])
    return InlineKeyboardButton(
                text="{} - {}".format(part, vote), callback_data=str(query_id) + hash(part)[:32])

def generate_buttons(query_id, parts, votes=None):
    if votes == None:
        votes = itertools.repeat(0, len(parts))

    return [[
        generate_button(query_id, part, vote)
    ] for part, vote in zip(parts, votes)]


def inline_handler(bot, update):
    query = update.inline_query.query

    if query == '':
        return

    try:
        parts = shlex.split(query)
    except ValueError as e:
        return

    query_id = update.inline_query.id

    if len(parts) == 1:
        update.inline_query.answer([
            InlineQueryResultArticle(
                id=query_id,
                title=parts[0],
                input_message_content=InputTextMessageContent(
                    "*{}*".format(parts[0]), parse_mode="markdown"))
        ])
    else:
        options = deduplicate(parts[1:])
        description = " / ".join(options)

        buttons = generate_buttons(query_id, options)
        update.inline_query.answer([
            InlineQueryResultArticle(
                id=query_id,
                title=parts[0],
                description=description,
                input_message_content=InputTextMessageContent(
                    generate_message(parts[0], options),
                    parse_mode="markdown"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        ])


def get_votes_for_option(option_id):
    return db_session.query(Vote.user_id).filter(
        Vote.option_id == option_id).group_by(Vote.user_id).count()


def update_message(bot, data, message_id):
    option_voted = Option.query.filter(Option.id == data).first()
    poll = option_voted.poll
    options = poll.options
    options_text = [option.title for option in poll.options]
    votes = [get_votes_for_option(option.id) for option in options]
    message = generate_message(poll.title, options_text, votes)
    bot.edit_message_text(
        text=message,
        inline_message_id=message_id,
        parse_mode="markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=generate_buttons(
            poll.id, options_text, votes)))


def button_handler(bot, update):
    query = update.callback_query
    votes = Vote.query.filter(Vote.user_id == query.from_user.id and
                              Vote.option_id == query.data)
    result = votes.order_by(Vote.id.asc()).first()
    if result is not None and result.option_id == query.data:
        votes.delete()
        update_message(bot, query.data, query.inline_message_id)
        return
    votes.delete()
    vote = Vote(
        id=time.time(), user_id=query.from_user.id, option_id=query.data)
    db_session.add(vote)
    db_session.commit()
    update_message(bot, query.data, query.inline_message_id)
    query.answer()

def deduplicate(seq):
    seen = set()
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]

def chosen_result_handler(bot, update):
    parts = shlex.split(update.chosen_inline_result.query)

    query_id = update.chosen_inline_result.result_id

    options = [
        Option(id=query_id + hash(part)[:32], title=part) for part in deduplicate(parts[1:])
    ]
    poll = Poll(
        id=query_id,
        title=parts[0],
        creator_id=update.chosen_inline_result.from_user.id,
        options=options)
    db_session.add(poll)
    db_session.commit()


if __name__ == "__main__":
    updater = Updater(token=TG_TOKEN)
    dispatcher = updater.dispatcher
    dispatcher.add_handler(InlineQueryHandler(inline_handler))
    dispatcher.add_handler(CallbackQueryHandler(button_handler))
    dispatcher.add_handler(ChosenInlineResultHandler(chosen_result_handler))
    dispatcher.add_error_handler(
        lambda bot, update, error: logging.error(error))
    updater.start_polling()
