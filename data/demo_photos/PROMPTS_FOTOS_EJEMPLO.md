# Fotos de los catálogos de ejemplo — método y prompts

> **Qué es esta carpeta.** Las fotos de los catálogos de **ejemplo** que se muestran en la
> tienda (`thecataloglink.com`). Todos los negocios de aquí son **ficticios**: existen para
> que alguien que llega sin conocernos vea cómo se ve un catálogo terminado.
>
> **🔴 REGLA DURA, la más importante de este archivo.** Nada de esta carpeta entra **jamás** al
> camino de cliente real ni a una vista previa de prospecto. Ahí van **siempre** las fotos
> reales del negocio, bajadas de su propio Instagram por Cory
> (`Cory/hmu-prospector/data/catalogo/prospects/<slug>/raw/`), con la regla de F2 de que un
> precio solo vale si aparece textual en su publicación. La venta en frío del producto es
> *"mira, este es **TU** catálogo, ya hecho"* — con una foto inventada eso es una mentira
> detectable en tres segundos, y se cae la promesa entera. Dos carpetas, dos caminos, sin
> puente. Vero preguntó explícitamente por esto el 2026-07-25; la respuesta es que **el plan
> no cambió**.

## Estado

| Negocio (ficticio) | Nicho | Estilo | Origen de las fotos | Estado |
|---|---|---|---|---|
| `dulce-marea` | Repostería | `vitrina-crema` | CC0 vía Openverse | ✅ 10 productos |
| `mesa-bonita` | Tablas / quesos | `revista-salvia` | CC0 vía Openverse | ✅ 8 productos |
| `casa-cacao` | Chocolatería | `elegante-onix` | CC0 vía Openverse | ✅ 8 productos |
| `estudio-amapola` | **Florería** | `revista-rosa` | **Generadas con IA** | ✅ 8 fotos · falta su `data/demos/*.json` |
| `piedra-luna` | **Joyería** | `elegante-medianoche` | **Generadas con IA** | ⬜ pendiente — prompts abajo |

**Por qué hubo que generar las dos últimas:** no existe fotografía de producto de florería ni
de joyería en corpus CC0. Se intentó dos veces; el corpus libre tira a museo y lámina del XIX.
**Decisión de Vero 2026-07-25:** no se compra pack de banco (Adobe/Shutterstock/iStock ≈ $29-30
USD/mes por 10 imágenes; Envato $33 el mes suelto con descarga ilimitada, pero exige *registrar
el uso* de cada foto mientras estás suscrita para que la licencia sobreviva a la cancelación).
Se generan con Gemini (Nano Banana).

**Elegir estilo sin repetir paleta:** `revista-rosa` y `elegante-medianoche` se escogieron a
propósito porque `mesa-bonita` ya usa `revista-salvia` y `casa-cacao` ya usa `elegante-onix`.
Dos ejemplos con la misma paleta se ven casi iguales y no demuestran nada nuevo.

## El método (esto es lo que hay que repetir)

1. **Genera la foto 1 desde cero** con el prompt completo, hasta que te guste.
2. **Las 7 restantes se piden en la MISMA conversación**, empezando con
   *"Using the previous images as a style reference — same shop, same light, same wall, same
   table — create a new product photo of a different item: …"*.
3. Si una sale con otra pared u otra luz: *"make it match the previous photos"* y de nuevo.

**Sin el paso 2 salen 8 tiendas distintas y el grid se ve revuelto.** Encadenar es lo único
que costó trabajo descubrir; el resto es copiar y pegar. Con florería funcionó a la primera:
las 8 comparten pared, ventana, mesa y hasta las macetas del fondo.

**Formatos:** pide `Vertical 4:5 format` o `Horizontal 3:2 format` explícitamente. Mezcla los
dos, como las fotos CC0. **Optimización:** entrégalas en PNG/JPG y pásalas por la receta F3
(`prospector/enhance_photos.py` → `optimize_to_webp`, 1080px / q82 / method 6). Florería fue
**61.10 MB → 0.58 MB (99% menos)**.

## ⚠️ Marca de agua — decisión tomada, no volver a plantearla

Las imágenes generadas traen la **estrella de 4 picos** que Google estampa (~90% del ancho,
~86% del alto) y la marca invisible **SynthID**.

**Decisión de Vero 2026-07-25: NO se quitan, ni recortando ni borrando.** Razones, en orden:

1. Al tamaño real de tarjeta (**360px**) es prácticamente invisible; se midió antes de decidir.
2. Recortar y borrar son **la misma acción**: quitar la señal de origen. Y la SynthID no se va
   de todos modos, así que solo dejaría la evidencia de que alguien lo intentó.
3. Contradice la promesa de la propia tienda, decidida el 07-24: contestar **arriba y de
   frente** de dónde salen las fotos. Es el único foso del producto.
4. Además el recorte habría destruido los formatos 4:5 / 3:2 y la uniformidad del grid, que es
   justo lo que costó lograr.

Si alguna vez se quieren sin la estrella visible, la vía limpia es **Google AI Studio**
(`aistudio.google.com`), que normalmente no la aplica y conserva la SynthID. Se le ofreció a
Vero y prefirió no rehacer las 8.

---

# `estudio-amapola` — florería ✅ HECHA

Categorías: **Ramos / Arreglos / Plantas**. Los 8 prompts textuales, foto por foto, están en el
`LICENSES.json` de `estudio-amapola/`, junto con el modelo, la fecha y el archivo que produjo
cada uno.

---

# `piedra-luna` — joyería ⬜ PENDIENTE

**Ojo: la joyería es el caso difícil.** Metal, piedras y reflejos es donde estos modelos
fallan, y sobre todo **inventan texto** (sellos, grabados, marcas). Cuenta con descartar más
que en florería. Las prohibiciones del bloque base son las que salvan la tanda — no las quites.

**Conversación NUEVA** (es otra tienda, otra luz). Foto 1 con el prompt completo; las 7
siguientes en la misma conversación.

### 1 — `aretes-argollas.jpg` · *Aretes* · 3:2

```
Photorealistic macro product photo for a small jewelry studio's online catalog. A pair of plain silver hoop earrings resting on grey linen, slightly apart. Handmade sterling silver, minimal modern design. Dark moody background: charcoal slate and deep grey velvet. Single soft diffused light source, controlled highlights on the metal, no blown-out reflections. Sharp focus on the piece. Absolutely no text, no engravings, no hallmarks, no brand stamps, no numbers on any surface. No hands, no models, no reflections of a camera or photographer. One clean piece, simple settings. Horizontal 3:2 format.
```

### 2 — `aretes-piedra-verde.jpg` · *Aretes* · 4:5

```
Using the previous images as a style reference — same jewelry studio, same single soft light, same charcoal slate and velvet background, same macro lens and mood — create a new product photo of a different item: a pair of drop earrings with a smooth green stone, hanging against the dark background. Absolutely no text, no engravings, no hallmarks, no brand stamps. No hands, no models. Vertical 4:5 format.
```

### 3 — `collar-dije.jpg` · *Collares* · 3:2

```
Using the previous images as a style reference — same jewelry studio, same light, same background — create a new product photo of a different item: a fine silver chain necklace with a small round pendant, laid in a loose curve on dark slate. Absolutely no text, no engravings, no hallmarks, no brand stamps. No hands, no models. Horizontal 3:2 format.
```

### 4 — `collar-perlas.jpg` · *Collares* · 4:5

```
Using the previous images as a style reference — same jewelry studio, same light, same background — create a new product photo of a different item: a baroque pearl necklace coiled on deep grey velvet, irregular natural pearls. Absolutely no text, no engravings, no hallmarks, no brand stamps. No hands, no models. Vertical 4:5 format.
```

### 5 — `anillo-martillado.jpg` · *Anillos* · 3:2

```
Using the previous images as a style reference — same jewelry studio, same light, same background — create a new product photo of a different item: a single hammered silver band ring standing upright on a dark stone surface, macro. Absolutely no text, no engravings, no hallmarks, no brand stamps. No hands, no models. Horizontal 3:2 format.
```

### 6 — `anillo-piedra-luna.jpg` · *Anillos* · 4:5

```
Using the previous images as a style reference — same jewelry studio, same light, same background — create a new product photo of a different item: a silver ring with a pale moonstone cabochon, resting on dark velvet, a soft glow inside the stone. Absolutely no text, no engravings, no hallmarks, no brand stamps. No hands, no models. Vertical 4:5 format.
```

### 7 — `collares-trio.jpg` · *Collares* · 3:2

```
Using the previous images as a style reference — same jewelry studio, same light, same background — create a new product photo of a different item: a flat lay of three delicate silver necklaces arranged in parallel on charcoal slate. Absolutely no text, no engravings, no hallmarks, no brand stamps. No hands, no models. Horizontal 3:2 format.
```

### 8 — `anillos-apilables.jpg` · *Anillos* · 4:5

```
Using the previous images as a style reference — same jewelry studio, same light, same background — create a new product photo of a different item: two thin stacking rings, one plain and one with a tiny stone, resting one against the other. Absolutely no text, no engravings, no hallmarks, no brand stamps. No hands, no models. Vertical 4:5 format.
```

## Al terminar la tanda

1. Convertir con la receta F3 a `piedra-luna/*.webp`.
2. Escribir su `LICENSES.json` con la misma forma que el de `estudio-amapola` (modelo, fecha,
   prompt por foto, la nota de marca de agua y la regla dura).
3. Crear `verticals/catalog/data/demos/piedra-luna.json` y `estudio-amapola.json` con el molde
   de las demos que ya existen (`business_name`, `brand_style`, `sale_button`, `content` ES/EN,
   `products[]` con categoría, nombre y descripción bilingües, precio y `image`).
   **Negocios ficticios: inventa dirección y teléfono, o déjalos fuera. Nunca los de un negocio
   real** — es el mismo error que se decidió corregir con `tablas-pintas` el 2026-07-25.
4. **1 sesión chica de Sonnet 5.** No bloquea nada.
