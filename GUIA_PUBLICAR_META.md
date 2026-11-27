# Publicar en Facebook e Instagram desde Clipear

Funciona con la API oficial de Meta y **tu propia app en modo desarrollo** (sin revisión de Meta, solo para tus cuentas). Los nombres de los menús de Meta cambian seguido: si algo no se llama igual, busca el equivalente.

## Antes de empezar
- Tu Instagram es Creator y está conectado a una Página de Facebook (ya lo tienes).
- Debes ser **administrador** de esa Página.
- Si Meta te pide «Autorización de publicación de la Página» (PPA), complétala en la Página; sin eso Instagram no deja publicar por API.

## Paso 1 · Crear tu app en Meta
1. Entra a https://developers.facebook.com con tu cuenta de Facebook y acepta ser desarrollador (puede pedir verificar tu teléfono).
2. **Mis apps → Crear app.** Ponle un nombre (ej. «Clipear personal») y tu correo.
3. Cuando pregunte qué quieres hacer (casos de uso), elige los de **administrar tu Página** y **contenido de Instagram**. Si te deja elegir tipo, usa «Empresa» (Business) u «Otro».
4. **No publiques la app**: déjala en **modo Desarrollo**. Así solo tú (como administrador de la app) puedes usarla, y es lo que queremos.
5. Ve a **Configuración de la app → Básica** y copia dos datos: **ID de la app** y **Clave secreta de la app** (botón «Mostrar»). No compartas la clave.

## Paso 2 · Sacar el token (una vez)
1. Abre el **Explorador de la API de Graph**: https://developers.facebook.com/tools/explorer
2. Arriba a la derecha elige **tu app**. En «Usuario o Página» deja **Token de acceso de usuario**.
3. En **Permisos**, agrega estos:
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_posts`
   - `instagram_basic`
   - `instagram_content_publish`
   - `business_management` (solo si tu Página pertenece a un Portafolio comercial / Business Manager; si no aparece ninguna Página después, agrégalo)
4. Pulsa **Generar token de acceso**. Se abre una ventana de Facebook: **selecciona tu Página y tu cuenta de Instagram**, y acepta todo.
5. Copia el token que aparece arriba (es largo). Es el «token corto»; Clipear lo cambia solo por uno largo.

## Paso 3 · Conectar en Clipear
1. Reemplaza `app.py` e `index.html` y reinicia el programa.
2. En **3 · Clips listos**, abre **«Conectar Facebook e Instagram»**.
3. Pega: ID de la app, clave secreta y el token → **Conectar**.
4. Elige tu Página en la lista (debe decir «Instagram @tuusuario») → **Usar esta Página**.
5. Se guarda en `data\social.json` (solo en tu PC). **No compartas ni subas esa carpeta a ningún lado.**

## Paso 4 · Publicar
1. En cada clip generado aparecen las casillas **Facebook** e **Instagram** y el botón **Publicar ahora**.
2. El texto del caption se puede editar justo ahí antes de publicar.
3. **Primera prueba:** solo Facebook con un clip de prueba; si sale bien, prueba solo Instagram; después ambos.
4. Pide confirmación antes de publicar y al terminar te muestra el enlace.

## Límites de Meta
- Facebook Reels (Páginas): 3 a 90 s, 9:16, mínimo 540×960 (los clips de Clipear en 9:16 cumplen); hasta 30 publicaciones por API cada 24 h.
- Instagram: hasta 100 publicaciones por API cada 24 h; solo cuentas Business/Creator; todo video publicado por API sale como Reel.
- Facebook solo publica Reels en **Páginas**, no en tu perfil personal.

## Si algo falla (copia el mensaje exacto que muestra Clipear)
- **«El token no ve ninguna Página»:** repite el Paso 2 marcando tu Página en la ventana de Facebook; agrega `business_management`.
- **Error de permisos (códigos 10 o 200):** revisa que estén los 5 permisos y que seas admin de la Página y de la app. Si aun así falla en Instagram, en **Roles de la app** agrega tu cuenta de Instagram como **Instagram Tester** y acepta la invitación desde Instagram (Configuración → Sitios web y apps).
- **Instagram pide PPA:** completa la autorización de publicación de la Página.
- **Token vencido más adelante:** repite los Pasos 2 y 3.
- **Error con la subida de Instagram:** esa parte (subida «resumable» directa a Meta) la documenta Meta para el inicio de sesión de Facebook, pero no la he podido probar con tu cuenta real; si falla, envíame el mensaje exacto.

## Instagram no aparece (solo se conecta la Página)
Clipear ahora avisa cuál es el motivo al pulsar «Conectar». Revisa en este orden:
1. **Faltan permisos:** si el aviso dice que no se concedieron `instagram_basic` o `instagram_content_publish`, genera el token otra vez (Paso 2) con esos permisos marcados. En la ventana de Facebook, en la lista de cuentas de Instagram, **marca tu cuenta**, no solo la Página.
2. **Instagram no está vinculado a ESA Página:** en Facebook entra a la Página «tu Página» → Configuración → **Cuentas vinculadas** (o «Instagram») → conecta tu cuenta de Instagram con su usuario y contraseña. Si el Instagram ya estaba unido a otra Página, desvincúlalo allí primero (una cuenta de Instagram solo puede estar en una Página).
3. **Desde Instagram:** Configuración → Cuenta de empresa/creador → Página conectada → elige «tu Página».
4. **Tu usuario no tiene permiso total sobre la Página:** debes ser administrador con acceso completo, no solo editor.
5. Después de arreglarlo, vuelve a hacer el Paso 2 (token nuevo) y el Paso 3 en Clipear (primero «Desconectar»).
