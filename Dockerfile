FROM python:3.13-slim

WORKDIR /app

RUN pip install -U discord.py openai

RUN mkdir -p /app/data

COPY bot.py describe.py ./

CMD ["python", "bot.py"]
