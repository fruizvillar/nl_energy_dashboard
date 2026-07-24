FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY p1serial.py /app/p1serial.py

CMD ["python", "-u", "/app/p1serial.py"]
