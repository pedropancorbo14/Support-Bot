<!--
PENDIENTE: pega aquí el system prompt.

Todo lo que esté dentro de comentarios HTML como este se elimina antes de enviarlo al
modelo. El fichero se relee automáticamente al cambiar, sin reiniciar el servicio.

Placeholders disponibles (se sustituyen tal cual; el resto de llaves no se toca):
  {app_name}     nombre de la app cliente
  {user_name}    nombre del usuario (JWT)
  {user_plan}    plan del usuario (JWT)
  {user_locale}  idioma del usuario (JWT)

Usa solo {app_name} si quieres aprovechar la caché de prefijo de Ollama: los datos del
usuario ya se añaden automáticamente justo después de este texto.

Detrás de este prompt el servicio añade, en este orden:
  ## Datos del usuario
  ## Documentación                        (fragmentos "[Documento > Sección] contenido")
  ## Resumen de la conversación anterior  (solo si el historial es largo)

Contrato de escalado: cuando haya que pasar con una persona, el modelo debe terminar su
respuesta con [ESCALAR: motivo]. El servicio quita la etiqueta antes de mostrar el mensaje.
-->
