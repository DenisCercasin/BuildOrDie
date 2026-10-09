from aiogram.fsm.state import State, StatesGroup


class RequestFlow(StatesGroup):
    book = State()
    format = State()
    deadline = State()
    budget = State()
    savings = State()
    review = State()
    interrupt = State()
