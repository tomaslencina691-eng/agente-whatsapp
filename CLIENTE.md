# Simplemente Fútbol: agente de WhatsApp y sistema mayorista

Esta copia del kit [dleodev-ia-agentkit](https://github.com/dleodev/dleodev-ia-agentkit) es la
que corre en producción. Los secretos no están acá: viven en las variables de entorno de Coolify.

## Dominios

| Dominio | Qué es |
|---|---|
| `mayoristasf.digital` | Web mayorista (catálogo y pedido por WhatsApp) |
| `app.mayoristasf.digital` | Panel de administración y stock del mayorista |
| `agente.mayoristasf.digital` | Webhook interno que usa Chatwoot para hablar con el agente |
| `crm.mayoristasf.digital` | Chatwoot (bandeja de conversaciones) |

## Qué se configura acá y qué no

- El stock del mayorista es propio (no el de la tienda): se carga en `app.mayoristasf.digital/admin`. De la tienda solo se traen los modelos nuevos.
- La web mayorista se edita desde el panel (pestaña «Web mayorista»): nombre, logo, color, textos de la portada, número de WhatsApp, mensaje del pedido, precios por cantidad, reglas, aviso, pausa de pedidos y más. Lo que no se tocó rige `mayorista/negocio.json`.
- `mayorista/negocio.json`: marca, pedido mínimo, precios por cantidad, categorías y condiciones (son los valores de arranque; el panel los pisa).
  Los precios están vacíos a propósito hasta que el negocio los confirme: sin precios, la web
  dice que el precio lo confirma una persona del equipo.
- `prompts/sistema.md`: la personalidad y las reglas del agente.
- Todo lo demás (claves, tokens, usuarios) va en Coolify, nunca en este repo.
