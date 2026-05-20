# Imagen oficial de Playwright: trae Chromium + dependencias del SO ya listas,
# matcheadas con playwright==1.48.0 (ver requirements.txt). Asi el PDF funciona
# sin tener que instalar el navegador a mano.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Dependencias primero para aprovechar la cache de capas de Docker.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render inyecta $PORT. Bind a 0.0.0.0 y --proxy-headers para que la app vea el
# esquema real (HTTPS) detras del proxy de Render: es clave para que el
# redirect_uri del OAuth se arme como https://.../callback y no http://.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=*"]
