FROM python:3.13-slim

WORKDIR /app

RUN pip install -U discord.py openai

COPY bot.py describe.py ./

CMD ["python", "bot.py"]
