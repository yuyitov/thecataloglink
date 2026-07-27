"""Engine-default UI strings (Fase 1.2, extended in Fase 1.6).

Base ES/EN copy for the WOW editorial template, shared by every vertical.
Per-vertical `strings_overrides` (from `verticals/<id>/vertical.yaml`) are
layered on top of this in `vertical_config.py` — see `build_strings()`.
Values marked *_html may contain trusted inline markup (<em>) and are
injected without re-escaping.

Fase 1.6 re-sync with HMU Link HEAD adds: `skip_to_content` (a11y skip link),
`price_ask` ("Consultar"/"Ask us" legend for price-less services),
`class_schedule_title`/`tour_details_title`/`pet_notes_title` (extra info-row
sections), `footer_privacy` (minimal legal footer link) and localizes the ES
`delivery_pickup_label` to "Entrega / recoger".

Fase 3.2 adds `lookbook_eyebrow`/`lookbook_title_html` for the optional
mini-lookbook block (ported from ModaLink, see blocks.py).
"""

from __future__ import annotations

BASE_STRINGS = {
    "es": {
        "title_suffix": "Servicios",
        "btn_whatsapp": "Reservar por WhatsApp",
        "btn_phone": "Llamar",
        "btn_email": "Enviar correo",
        "btn_website": "Sitio web",
        "btn_booking": "Reservar en linea",
        "btn_maps": "Google Maps",
        "btn_other": "Abrir enlace",
        "gallery_prev": "Foto anterior",
        "gallery_next": "Foto siguiente",
        "gallery_photo": "Foto",
        "lookbook_eyebrow": "Galería",
        "lookbook_title_html": "Mini <em>lookbook</em>",
        "delivery_pickup_label": "Entrega / recoger",
        "portfolio_label": "Portafolio",
        "view_menu": "Ver servicios",
        "scroll_hint": "Desliza",
        "skip_to_content": "Saltar al contenido",
        "services_eyebrow": "Servicios",
        "menu_title_html": "Nuestros <em>servicios</em>",
        "services_fallback": "Servicios",
        "price_ask": "Consultar",
        "featured_badge": "Destacado",
        "visit_eyebrow": "Detalles",
        "visit_title_html": "Planea tu <em>visita</em>",
        "hours_title": "Horarios",
        "client_care_title": "Atencion",
        "reservations_title": "Reservaciones",
        "address_title": "Dónde estamos",
        "address_map": "Ver en Google Maps",
        "location_label": "Ubicacion",
        "service_area_title": "Area de servicio",
        "class_schedule_title": "Horario de clases",
        "tour_details_title": "Sobre la experiencia",
        "pet_notes_title": "Antes de tu visita",
        "policies_title": "Políticas",
        "links_title": "Enlaces",
        "faq_eyebrow": "FAQ",
        "faq_title_html": "Preguntas <em>frecuentes</em>",
        "share_eyebrow": "Comparte",
        "share_title_html": "Llévanos <em>contigo</em>",
        "share_lead": "Escanea el código o comparte el link directo.",
        "share_button": "Compartir link",
        "share_copied": "Link copiado",
        "footer_credit": "Hecho con {brand_name}",
        "footer_privacy": "Privacidad",
        "footer_demo_credit": "Demo hecho con {brand_name}",
        "qr_alt": "Codigo QR de",
        "lang_switch": "View this page in English",
        # --- Catálogo (plantilla F4). Claves solo usadas por la plantilla
        # catalog; la plantilla service-menu no las referencia, así que
        # agregarlas no toca el golden de HMU. ---
        "catalog_title_suffix": "Catálogo",
        "catalog_search_placeholder": "Buscar en el catálogo…",
        "catalog_search_label": "Buscar productos",
        "catalog_filter_all": "Todo",
        "catalog_no_results": "No encontramos productos con esa búsqueda.",
        "catalog_products_eyebrow": "Catálogo",
        "catalog_products_title_html": "Nuestros <em>productos</em>",
        # "Preguntar precio" es la NORMA en este nicho (los negocios chicos no
        # publican precio en IG), no un hueco: se muestra como una pastilla
        # intencional, no como un error.
        "catalog_ask_price": "Preguntar precio",
        "catalog_sale_whatsapp": "Pedir por WhatsApp",
        "catalog_sale_sms": "Pedir por mensaje",
        "catalog_sale_tel": "Llamar para pedir",
        "catalog_sale_generic": "Hacer un pedido",
        "catalog_product_cta": "Preguntar por este",
        "catalog_results_count_one": "1 producto",
        # {{n}} sobrevive el .format(brand_name=...) de build_strings como el
        # literal {n}, que el JS del catálogo reemplaza por el conteo.
        "catalog_results_count_many": "{{n}} productos",
    },
    "en": {
        "title_suffix": "Services",
        "btn_whatsapp": "Book on WhatsApp",
        "btn_phone": "Call",
        "btn_email": "Send an email",
        "btn_website": "Website",
        "btn_booking": "Book online",
        "btn_maps": "Google Maps",
        "btn_other": "Open link",
        "gallery_prev": "Previous photo",
        "gallery_next": "Next photo",
        "gallery_photo": "Photo",
        "lookbook_eyebrow": "Gallery",
        "lookbook_title_html": "Mini <em>lookbook</em>",
        "delivery_pickup_label": "Delivery / pickup",
        "portfolio_label": "Portfolio",
        "view_menu": "See services",
        "scroll_hint": "Scroll",
        "skip_to_content": "Skip to content",
        "services_eyebrow": "Services",
        "menu_title_html": "Our <em>services</em>",
        "services_fallback": "Services",
        "price_ask": "Ask us",
        "featured_badge": "Featured",
        "visit_eyebrow": "Details",
        "visit_title_html": "Plan your <em>visit</em>",
        "hours_title": "Hours",
        "client_care_title": "How we work",
        "reservations_title": "Reservations",
        "address_title": "Where we are",
        "address_map": "View on Google Maps",
        "location_label": "Location",
        "service_area_title": "Service area",
        "class_schedule_title": "Class schedule",
        "tour_details_title": "About the experience",
        "pet_notes_title": "Before your visit",
        "policies_title": "Policies",
        "links_title": "Links",
        "faq_eyebrow": "FAQ",
        "faq_title_html": "Common <em>questions</em>",
        "share_eyebrow": "Share",
        "share_title_html": "Take us <em>with you</em>",
        "share_lead": "Scan the code or share the direct link.",
        "share_button": "Share link",
        "share_copied": "Link copied",
        "footer_credit": "Made with {brand_name}",
        "footer_privacy": "Privacy",
        "footer_demo_credit": "Demo made with {brand_name}",
        "qr_alt": "QR code for",
        "lang_switch": "Ver esta página en español",
        # --- Catalog (F4 template). Keys used only by the catalog template. ---
        "catalog_title_suffix": "Catalog",
        "catalog_search_placeholder": "Search the catalog…",
        "catalog_search_label": "Search products",
        "catalog_filter_all": "All",
        "catalog_no_results": "No products match that search.",
        "catalog_products_eyebrow": "Catalog",
        "catalog_products_title_html": "Our <em>products</em>",
        # "Ask for price" is the NORM in this niche (small shops don't post
        # prices on IG), not a gap: shown as an intentional pill, not an error.
        "catalog_ask_price": "Ask for price",
        "catalog_sale_whatsapp": "Order on WhatsApp",
        "catalog_sale_sms": "Order by text",
        "catalog_sale_tel": "Call to order",
        "catalog_sale_generic": "Place an order",
        "catalog_product_cta": "Ask about this",
        "catalog_results_count_one": "1 product",
        "catalog_results_count_many": "{{n}} products",
    },
}

# --------------------------------------------------------------------------- #
# ES→EN de CATEGORÍAS del catálogo (F4 paso 3).
# --------------------------------------------------------------------------- #
# F2 (extract_products) entrega la categoría del producto SOLO en español (es
# una etiqueta corta por tipo de producto: "Tablas", "Ramos", "Talleres"…). El
# badge y el chip del catálogo deben salir en inglés cuando la página está en
# inglés (mismo espíritu i18n de la fábrica). Como las categorías son abiertas
# (las genera el modelo), no caben en BASE_STRINGS: este es un diccionario de
# traducción por VOCABULARIO del nicho, consultado en tiempo de render.
#
# Clave = categoría en español NORMALIZADA (minúsculas, sin acentos, sin
# espacios extra — la misma normalización que `_plain` en generate_catalog).
# Se cubren singular y plural porque la normalización NO singulariza. Si una
# categoría no está aquí, el generador cae de forma elegante a la etiqueta en
# español (mejor una palabra en español que un hueco). Ampliar esta tabla es
# aditivo y no toca a ninguna otra vertical (no forma parte de BASE_STRINGS).
CATALOG_CATEGORY_EN = {
    # genéricos / transversales
    "talleres": "Workshops", "taller": "Workshop",
    "cursos": "Courses", "curso": "Course",
    "clases": "Classes", "clase": "Class",
    "regalos": "Gifts", "regalo": "Gift",
    "detalles": "Gifts", "detalle": "Gift",
    "cajas": "Boxes", "caja": "Box",
    "canastas": "Baskets", "canasta": "Basket",
    "combos": "Combos", "combo": "Combo",
    "paquetes": "Packages", "paquete": "Package",
    "promociones": "Deals", "promocion": "Deal",
    "temporada": "Seasonal", "temporadas": "Seasonal",
    "accesorios": "Accessories", "accesorio": "Accessory",
    "personalizados": "Personalized", "personalizado": "Personalized",
    # repostería / postres
    "pasteles": "Cakes", "pastel": "Cake",
    "postres": "Desserts", "postre": "Dessert",
    "galletas": "Cookies", "galleta": "Cookie",
    "cupcakes": "Cupcakes", "cupcake": "Cupcake",
    "gelatinas": "Jellies", "gelatina": "Jelly",
    "panques": "Loaf cakes", "panque": "Loaf cake",
    "pan": "Bread", "panes": "Breads", "panaderia": "Bakery",
    "cheesecakes": "Cheesecakes", "cheesecake": "Cheesecake",
    "reposteria": "Pastries", "brownies": "Brownies", "brownie": "Brownie",
    # fresas / chocolate
    "fresas con chocolate": "Chocolate strawberries",
    "chocofresas": "Chocolate strawberries",
    "fresas": "Strawberries", "fresa": "Strawberry",
    "chocolates": "Chocolates", "chocolate": "Chocolate",
    "antojos": "Treats", "antojo": "Treat",
    # florería
    "ramos": "Bouquets", "ramo": "Bouquet",
    "arreglos": "Arrangements", "arreglo": "Arrangement",
    "arreglos florales": "Floral arrangements",
    "flores": "Flowers", "flor": "Flower",
    "coronas": "Wreaths", "corona": "Wreath",
    "plantas": "Plants", "planta": "Plant",
    "suculentas": "Succulents", "suculenta": "Succulent",
    "globos": "Balloons", "globo": "Balloon",
    # charcutería / gourmet
    "tablas": "Boards", "tabla": "Board",
    "charcuteria": "Charcuterie",
    "quesos": "Cheeses", "queso": "Cheese",
    "snacks": "Snacks", "snack": "Snack",
    "desayunos": "Breakfasts", "desayuno": "Breakfast",
    "bebidas": "Drinks", "bebida": "Drink",
    "gourmet": "Gourmet",
    # joyería / velas / perfumería (Elegante)
    "joyeria": "Jewelry", "joyas": "Jewelry", "joya": "Jewelry",
    "aretes": "Earrings", "arete": "Earring",
    "collares": "Necklaces", "collar": "Necklace",
    "anillos": "Rings", "anillo": "Ring",
    "pulseras": "Bracelets", "pulsera": "Bracelet",
    "dijes": "Charms", "dije": "Charm",
    "relojes": "Watches", "reloj": "Watch",
    "velas": "Candles", "vela": "Candle",
    "jabones": "Soaps", "jabon": "Soap",
    "perfumes": "Perfumes", "perfume": "Perfume",
    "aromas": "Scents", "aroma": "Scent",
    "bisuteria": "Costume jewelry",
}
