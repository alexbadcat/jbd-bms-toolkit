# jbd2mqtt у контейнері. Потребує прямого доступу до /dev/ttyUSB0 (або
# /dev/rs485-bms, якщо розгорнуто udev-правило deploy/udev/99-rs485.rules) —
# дивись docker-compose.yml, секція devices.
FROM python:3.12-slim

WORKDIR /opt/jbd-bms-toolkit

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "src/jbd2mqtt.py"]
