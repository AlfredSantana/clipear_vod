# Clipear

Herramienta local para sacar **clips verticales** (TikTok, Reels, Shorts) de videos largos. Corre en tu propia computadora y se usa desde el navegador.

Qué hace:
- Toma un **link** (YouTube u otra plataforma), un **video de tu computadora** o un **directo en vivo**.
- Transcribe el audio (Whisper) y una **IA** (Gemini) propone los mejores momentos.
- Editor de clips: formato 9:16 y otros, cámara que sigue a quien habla, subtítulos, título, logo, pantalla dividida, modo streamer.
- Genera los clips en alta calidad y, si quieres, los publica en Facebook e Instagram.
- En el **modo directo** graba la transmisión mientras ocurre y va sacando clips solo.

> Todo se procesa en tu computadora. Los videos no se suben a ningún servidor, salvo el texto de la transcripción que se envía a la IA que elijas (Gemini).

---

## 1. Qué necesitas

| Requisito | Detalle |
|---|---|
| Sistema | Windows 10 u 11 |
| Python | **3.11 o 3.12** (de python.org) |
| ffmpeg | Se instala con `Instalar.bat` |
| Deno | Lo necesita YouTube para dar los formatos de video. Se instala con `Instalar.bat` |
| API key de Gemini | Gratis, la creas tú (paso 4) |
| Tarjeta NVIDIA (opcional) | Con ella todo es mucho más rápido. Sin ella funciona igual en el procesador, pero más lento |
| Espacio en disco | Unos **10 GB libres** para empezar (modelos y videos temporales). Un directo de 2 h en 1080p ocupa varios GB |
| Internet | Para la instalación, para bajar videos y para la IA |

---

## 2. Instalación (una sola vez)

1. **Instala Python.** Entra a https://www.python.org/downloads/ y baja Python 3.12. Al instalar, **marca la casilla «Add python.exe to PATH»** (sale en la primera pantalla).
2. **Baja Clipear.** En esta página de GitHub pulsa el botón verde **Code → Download ZIP**, y descomprime la carpeta donde quieras (por ejemplo `C:\Clipear`). Evita rutas con carpetas de OneDrive o nombres muy raros.
3. **Haz doble clic en `Instalar.bat`.** Crea el entorno de Python, instala las librerías y, si faltan, instala ffmpeg y Deno con winget. Tarda varios minutos. Si al final dice que se instalaron programas nuevos, **cierra esa ventana**.
4. **Consigue tu API key de Gemini** (gratis):
   - Entra a https://aistudio.google.com/apikey con tu cuenta de Google.
   - Pulsa «Create API key» y copia la clave.
   - La pegarás en Clipear; se guarda **solo en tu navegador**.

La primera vez que analices un video, Clipear descarga solo los modelos de transcripción, de caras y de voces. Es lento esa primera vez (varios cientos de MB); las siguientes ya no.

---

## 3. Uso diario

1. Haz doble clic en **`Iniciar.bat`**. Se abre una ventana negra (no la cierres mientras uses la herramienta) y, a los segundos, el navegador en `http://127.0.0.1:5000`.
2. Pega tu API key en **«1 · Video y análisis»**.
3. Elige cómo empezar:
   - **Link de internet:** pega el link y pulsa **Analizar video**. Puedes poner Desde/Hasta para analizar solo un tramo.
   - **Video de mi computadora:** elige «Video de mi computadora», pulsa «Elegir video…» y analiza.
   - **Directo en vivo:** marca «Es un directo», pega el link del directo y pulsa **Empezar a grabar**. Los clips van apareciendo en el editor mientras sigue grabando. Pulsa **Detener** cuando quieras.
4. **Edita los clips** (formato, cámara, subtítulos, título, logo). Cada pestaña se puede aplicar a todos los clips o solo a uno.
5. Pulsa **Generar clips seleccionados**. Salen en alta calidad y puedes descargarlos o publicarlos.

### Contexto para la IA (recomendado)
En la tarjeta 1 hay una sección «Contexto para la IA». Escribe de qué es el programa y quién es cada participante; la IA usará esos nombres en los títulos y captions. Puedes guardarlo como perfil.

### Cookies de YouTube
Déjalo en **Ninguno**. Solo si YouTube te pide «confirmar que no eres un robot», elige tu navegador (Firefox funciona mejor que Chrome) con la sesión de YouTube iniciada.

### Publicar en Facebook e Instagram (opcional)
Necesitas crear **tu propia app de Meta** (gratis, en modo desarrollo) y conectar tus Páginas. Sigue `GUIA_PUBLICAR_META.md`. Se pueden conectar varias Páginas y elegir dónde publicar cada clip.

---

## 4. Dónde se guarda todo

Todo queda dentro de la carpeta del programa, en `data\`:
- `data\clips` — los clips generados.
- `data\models` — modelos descargados.
- `data\social.json` — **tus tokens de Meta (privado)**.
- `data\jobs`, `data\cache` — archivos temporales; el programa los limpia solo.

> **Nunca compartas ni subas la carpeta `data`.** Por eso el archivo `.gitignore` la excluye de GitHub.

Para actualizar: baja la nueva versión y reemplaza `app.py` e `index.html`. Tu carpeta `data` no se toca.

---

## 5. Problemas comunes

| Mensaje o síntoma | Qué hacer |
|---|---|
| `No video formats found` o `Requested format is not available` | Cierra Clipear y en la ventana negra de instalación ejecuta `pip install -U yt-dlp`. Verifica que Deno esté instalado (`deno --version`). Cierra y abre Clipear de nuevo |
| `No encuentro ffmpeg` | Ejecuta `Instalar.bat` otra vez, cierra la ventana y vuelve a abrir `Iniciar.bat` |
| `Falta la instalación` al abrir `Iniciar.bat` | Ejecuta primero `Instalar.bat` |
| Va muy lento | Sin tarjeta NVIDIA todo corre en el procesador. En la tarjeta 1, usa el modelo de transcripción «small» |
| El directo graba 0:00 | Revisa el mensaje que sale en la barra de estado y que el link sea un directo activo (no terminado, ni solo para miembros) |
| La IA falla con un error de cuota | La versión gratis de Gemini tiene límites por minuto y por día; espera un poco o usa otro modelo |
| Para voces más precisas | `Instalar.bat` ya instala `sherpa-onnx`; si falta, ejecuta `pip install sherpa-onnx` dentro del entorno (`venv\Scripts\activate`) |

Si algo no funciona, copia el mensaje exacto que sale en pantalla o en la ventana negra; casi siempre dice qué pasó.

---

## 6. Aviso

Usa la herramienta solo con contenido que tengas derecho a usar. Descargar o publicar videos de otras personas puede violar los términos de las plataformas o los derechos de autor; eso es responsabilidad de quien la use.
