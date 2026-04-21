# Estado del CLOB de Polymarket y viabilidad del market making puro en 2026

## 1. Arquitectura actual del CLOB y cambios 2025–2026

Polymarket sigue utilizando un modelo de Central Limit Order Book (CLOB) híbrido: matching de órdenes off‑chain y liquidación on‑chain a través de un contrato Exchange auditado, donde las órdenes son mensajes EIP‑712 firmados y el operador sólo puede emparejar órdenes, no fijar precios ni mover fondos sin autorización. La documentación de 2026 describe explícitamente este diseño CLOB, con APIs REST y WebSocket para leer libro, precios, midpoints y spreads, lo que indica que no han vuelto a un modelo AMM.[1][2][3]

En 2025 se introdujeron varias mejoras de infraestructura sobre el CLOB existente: aumento de límites de peticiones para endpoints de /books y /price, endpoint de órdenes en batch (hasta 5 inicialmente, luego 15), nuevos campos en get‑book(s) como tick_size, min_order_size y neg_risk, y cambios en el canal WebSocket (eliminación del límite de 100 tokens y flag initial_dump para recibir el estado inicial del libro). Estas son optimizaciones de rendimiento y ergonomía para traders y bots, pero no cambios de modelo de mercado.[4]

A principios de 2026, Polymarket anunció la mayor actualización de infraestructura desde su lanzamiento: CTF Exchange V2 y CLOB v2, junto con la migración de colateral de USDC.e a un token estable propio, Polymarket USD, respaldado 1:1 por USDC. CLOB v2 introduce una estructura de orden simplificada, matching más rápido, soporte para firmas EIP‑1271 (wallets smart contract), “builder codes” para atribución on‑chain y lógica de fees rediseñada; los SDK oficiales en TypeScript, Python y Go gestionarán la transición de V1 a V2, mientras que los bots custom tendrán que actualizar SDK y re‑firmar órdenes. Esta actualización, en despliegue durante abril de 2026, refuerza el modelo CLOB en lugar de reemplazarlo.[5][6][7]

## 2. ¿Ha habido un giro hacia AMM o liquidez externa?

Polymarket documenta explícitamente que usa un CLOB y contrasta este modelo con AMM: el precio emerge del libro de órdenes y no de una fórmula de pool de liquidez. Artículos de análisis de MetaMask sobre spreads en mercados de predicción subrayan que Polymarket es un ejemplo de arquitectura CLOB, frente a plataformas que usan AMM tipo bonding curve, y atribuyen a este cambio la observación de spreads más ajustados en sus contratos más líquidos.[8][9][1]

Guías técnicas y skills de terceros (LobeHub, OpenClaw, bots en GitHub) tratan la API de Polymarket como un CLOB puro que expone profundidad L2, midpoints y gestión completa de órdenes, apoyando que el modelo actual es order book nativo y no un AMM con puente de liquidez. No hay evidencia reciente de que Polymarket esté externalizando la formación de precios a pools AMM externos; más bien, la tendencia es lo contrario: CLOB v2, API institucional de order book para Polymarket US y herramientas específicas para market makers.[10][11][12][13][5]

## 3. Microestructura del CLOB: tipos de órdenes, ticks, delays y matching

La especificación de órdenes de 2026 contempla sólo órdenes límite como primitiva subyacente, sobre las que se construyen comportamientos de “market order” mediante límites cruzados. Se soportan tipos GTC, GTD, FOK y FAK, además de órdenes post‑only, con validaciones de tick_size, tamaño mínimo, balances y allowances on‑chain, y un mecanismo de heartbeat que cancela todas las órdenes abiertas si no recibe señales de vida en ~10 segundos.[14]

La documentación de órdenes y orderbook fija cuatro tamaños de tick por mercado: 0.1, 0.01, 0.001 y 0.0001, con ejemplos explícitos de precios como 0.001/0.999 para ticks de 0.001 o 0.0001. Un artículo técnico de MetaMask señala que Polymarket adapta la granularidad del tick para permitir quoting muy fino cerca de los extremos (por encima de 0.96 o por debajo de 0.04), ayudando a observar spreads sub‑centavo en los contratos más líquidos.[9][3][14]

En deportes se introdujo una demora de 3 segundos en la ejecución de market orders, diseñada explícitamente para proteger a los market makers frente a traders con datos ultra‑rápidos y reducir el riesgo de arbitraje de información. Por otro lado, un artículo de exchange‑news documenta que en febrero de 2026 se eliminó un delay de 500 ms que afectaba a órdenes taker, facilitando ejecuciones más rápidas para estrategias de trading de alta frecuencia. Polymarket también documenta un protocolo de “matching engine restarts” en el que la API de CLOB devuelve HTTP 425 durante reinicios y recomienda a los bots reintentar con backoff exponencial, coordinando estos cambios a través del canal #trading‑apis en Discord.[15][16][9]

## 4. Evolución de incentivos: de liquidity mining generalista a fees + rebates dirigidos

En 2022 Polymarket ya había lanzado programas de liquidity mining en colaboración con UMA para bootstrapear su nuevo order book, usando grants en UMA para incentivar market makers en CLOB. Sin embargo, el esquema actual de incentivos (2025–2026) está mucho más estructurado y centrado en rebates financiados por fees y grandes pools de recompensas dirigidos a categorías concretas.[17]

En 2025, análisis de AInvest y otros medios destacan que Polymarket destinó del orden de 12 millones de dólares en recompensas a proveedores de liquidez durante el año, apoyándose en un modelo de maker rebates financiado con taker fees y alineado con métricas de volumen y fee‑equivalent. La documentación oficial de “Liquidity Rewards” explica un sistema de puntos por orden resting basado en dos‑sided depth y tightness frente al midpoint, usando una función cuadrática S(v,s) y tomando el mínimo entre las dos caras del libro (Q_one, Q_two), inspirado en el programa de dYdX.[18][19]

A partir de enero de 2026, el changelog muestra una aceleración clara:

- 5 de enero de 2026: activación de taker fees y maker rebates en mercados de cripto de 15 minutos, con fees que alcanzan un máximo de 1.56% cerca del 50% de probabilidad.[4]
- 11 de febrero de 2026: extensión de taker fees y maker rebates a NCAAB (baloncesto universitario) y Serie A, con cálculo de rebates por mercado (los makers sólo compiten dentro de cada mercado).[4]
- 12 de febrero de 2026: lanzamiento de mercados de cripto de 5 minutos con misma curva de fees y acceso a rebates.[4]
- 1 de marzo de 2026: expansión de taker fees y maker rebates a todos los mercados de cripto (1H, 4H, diarios y semanales) para nuevos mercados creados a partir del 6 de marzo.[4]
- 30 de marzo de 2026: Fee Structure V2, aplicando fees a Crypto, Sports, Finance, Politics, Economics, Culture, Weather, Tech, Mentions y Other/General; los mercados Geopolitics siguen libres de fees.[4]

La página de “Maker Rebates Program” (actualizada en 2026) especifica porcentajes de rebate por categoría (20% de taker fees en Crypto, 25% en Sports, Finance, Politics, etc.) y confirma que los rebates se calculan y pagan diariamente en USDC, proporcionales al fee_equivalent generado por cada maker en cada mercado. Complementariamente, el help center de Polymarket describe un Maker Rebates Program específico para mercados de cripto de 15 minutos orientado a hacerlos «más profundos, más ajustados y más fáciles de operar», con ejemplos de traders que obtienen retornos muy altos apalancando estas recompensas.[20][21][22]

En paralelo, Polymarket ha lanzado grandes campañas de liquidity rewards no basadas sólo en taker fees sino en pools pre‑asignados. La documentación oficial detalla un programa de más de 5 millones de dólares en incentivos de liquidez para abril de 2026, concentrado en mercados de deportes y esports (fútbol, NBA, ligas regionales, CS2, LoL, Dota2, Valorant, etc.), con cantidades por partido que llegan a 24 000 dólares en Champions League y 7700 dólares en partidos NBA. Noticias de BlockBeats y otros medios replican estas cifras y remarcan el foco en Sports/Esports durante abril.[23][24][18]

## 5. Evidencia de actividad de market makers y spreads ajustados

### 5.1. Spreads observados en la práctica

Un análisis de MetaMask sobre spreads de 5 centavos en prediction markets, publicado en abril de 2026, documenta empíricamente que:

- En Polymarket y Kalshi se observan spreads de 1–2 centavos en mercados de alta liquidez con market makers activos, típicamente grandes eventos deportivos y contratos políticos de alto perfil.[9]
- Mercados de liquidez media (10 000–100 000 dólares de volumen diario) suelen mostrar spreads de 3–5 centavos; ejemplos incluyen decisiones concretas de tipos de la Fed y mercados de precio de cripto de capitalización media.[9]
- Mercados con bajo volumen (menos de 10 000 dólares al día) presentan spreads de 6–10 centavos o incluso superiores a 10 centavos, momento en el que Polymarket deja de mostrar el midpoint y pasa a mostrar el último precio negociado.[9]

El mismo artículo subraya que algunos contratos de Polymarket presentan spreads sub‑centavo (por debajo de 0.01) gracias a ticks tan finos como 0.0001, especialmente en elecciones y enfrentamientos deportivos de máxima popularidad donde operan market makers profesionales. También cita un informe de FalconX de febrero de 2026 que encuentra que los spreads de Polymarket tienden a estrecharse a medida que los contratos de mayor duración se acercan a la resolución, aunque la mayoría de mercados en la muestra tenían menos de dos semanas de historia antes de resolverse.[9]

### 5.2. Bots, infra y research específicos para MM en Polymarket

La existencia de bots, SDKs y research especializado alrededor del CLOB de Polymarket es otra evidencia de actividad continuada de market makers:

- Repositorios como `polymarket-tracker-bot` y motores de ejecución low‑latency en Rust publicados en 2025–2026 describen engines headless que se conectan directamente al CLOB, monitorizan orderbook en tiempo real y ejecutan estrategias de day‑trading y copy‑trading.[25][11]
- Skills en LobeHub y OpenClaw exponen el API de CLOB de Polymarket como una fuente de datos de precios en tiempo real, profundidad, midpoints y gestión de órdenes, recomendándolos explícitamente para construir bots de market making y arbitraje.[13][10]
- Un artículo extenso de Weex/Daedalus Research (marzo de 2026) presenta un “market‑making bible” específica para Polymarket, que modela la dinámica de probabilidades en logit space, define Greeks como Delta = p(1−p) y propone un framework tipo Avellaneda‑Stoikov adaptado al CLOB de Polymarket para decidir spreads y gestionar inventario. El artículo destaca que Polymarket ya contaba con más de 10 millones de dólares en fondos de incentivos para market makers y que varios equipos cuantitativos estaban aplicando modelos de volatilidad implícita y correlación para hacer market making en la plataforma.[26]

Hilos en Reddit y blogs de usuarios muestran casos concretos de traders que presumen de rentabilidades muy altas en un día (por ejemplo, 44% en un día) usando el nuevo sistema de rebates de Polymarket, así como análisis de coste efectivo de trading que recomiendan operar como maker (órdenes límite) en lugar de taker. Todo ello apunta a que hay market makers activos y sofisticados, pero concentrados en subconjuntos de mercados muy concretos.[22][27]

## 6. Concentración de liquidez por tipo de mercado y horizonte

### 6.1. Sports y esports vs cripto y nicho

Un análisis de PANews de enero de 2026 sobre 295 000 datos históricos de mercados de Polymarket concluye que:

- Más del 60% de los mercados de muy corto plazo tienen 0 volumen en 24 horas, y un 63.16% de contratos en mercados activos menos de un día registran cero volumen diario.[28]
- Los mercados de cripto de corto plazo muestran una liquidez muy débil, con un volumen medio de 44 000 dólares, mientras que los mercados deportivos de corto plazo tienen un volumen medio de 1.32 millones de dólares, unas 30 veces más.[28]
- Sólo 505 contratos superan los 10 millones de dólares de volumen, pero representan el 47% del volumen total de la plataforma, indicando una concentración extrema en unos pocos “narratives” dominantes.[28]

El análisis concluye que Polymarket está evolucionando hacia una plataforma caracterizada por “high‑frequency sports betting” y “macro‑political hedging”, con la mayor parte de la liquidez concentrada en pocos temas de alto perfil. Esto encaja con la estructura de incentivos vigente: los grandes programas de rewards de abril de 2026 se concentran en Sports/Esports, y la expansión de fees + rebates en cripto intenta reanimar una categoría corta de liquidez pero no garantiza que todos los mercados cripto tengan profundidad real.[18][28][4]

### 6.2. Política y macro a largo plazo

Investigaciones de Kaiko sobre prediction markets apuntan a que Polymarket procesó más de 2000 millones de dólares de volumen ligado a la elección presidencial de 2024, pero el open interest cayó de unos 1000 millones a 200 millones tras las elecciones, planteando dudas sobre la sostenibilidad del interés en política fuera de ciclos electorales. Sin embargo, la misma fuente y otros análisis señalan que los mercados políticos de EE. UU. de largo plazo siguen concentrando la mayor liquidez dentro de los mercados de “macro‑política”, con volúmenes medios por mercado muy superiores a los de sectores de nicho.[29][30][28]

La guía de MetaMask confirma que, en abril de 2026, Polymarket y Kalshi son los dos mayores venues de prediction markets, y que Polymarket mantiene liderazgo histórico en política y macro, con spreads que se estrechan a medida que se acerca la resolución en estos contratos emblemáticos. Documentación para Polymarket US describe además un Order Book API institucional para eventos macro (CPI, empleo, etc.) y un programa de liquidity rewards específicos para eventos como CPI year‑over‑year, con 10 000 dólares por día en incentivos de liquidez.[31][12][9]

### 6.3. Estructura temporal y calendario

Los datos de PANews muestran que los mercados de predicciones de largo plazo (duración superior a 30 días) tienen una liquidez media 45 veces superior a la de mercados de un solo día. Tras grandes eventos (p.ej., elecciones presidenciales), el interés se desplaza hacia nuevos “macro narratives”, pero la liquidez sigue fuertemente sesgada hacia unos pocos mercados estrella.[30][28]

Además, campañas como “March Madness Liquidity Rewards” añaden más de 2 millones de dólares en incentivos durante torneos específicos de NCAAB, con pagos diarios de hasta 60 000 dólares en el moneyline live de cada partido, lo que refuerza una fuerte estacionalidad de profundidad ligada a eventos deportivos concretos. En períodos donde la cartelera está dominada por outrights deportivos de muy largo plazo (ganador de liga, máximo goleador, etc.), estos suelen mostrar profundidad mucho menor que partidos individuales con incentivos activos, lo que cuadra con la presencia de libros con bid≈0.001 y ask≈0.999 pero sin volumen real intermedio.[4]

## 7. Calidad y accesibilidad de datos de order book

Un hilo de abril de 2026 en r/PredictionMarkets explica que el order book de Polymarket se gestiona off‑chain por el matching engine, y sólo los fills quedan registrados on‑chain a través de eventos OrderFilled en los contratos CTF y NegRisk. Como consecuencia, el estado histórico del libro (depth por nivel de precio) no se almacena ni en chain ni en la API; si no se capturó por WebSocket en tiempo real, simplemente “no existe”.[32]

El mismo usuario indica que lleva grabando el order book completo desde noviembre de 2025, con datos de alta resolución desde marzo de 2026, observando que mercados activos pueden generar del orden de 1000 actualizaciones por segundo en picos, lo que hace que el almacenamiento y consulta sean desafiantes. Este contexto implica que la mayor parte de análisis públicos sobre spreads y liquidez se basan en snapshots puntuales, no en reconstrucciones históricas completas, y que los bots que no mantengan un feed WebSocket robusto pueden tener una visión muy parcial del verdadero depth disponible en cada momento.[32]

## 8. Factores adicionales que afectan a la estructura de liquidez

### 8.1. Market making interno de Polymarket

Un análisis de AInvest de diciembre de 2025 afirma que Polymarket ha desarrollado un equipo interno de market making, similar al de Kalshi, para estabilizar la liquidez en ciertos mercados clave, lo que genera debate sobre posibles conflictos de interés pero indica una participación activa del “house” en la provisión de liquidez. El artículo vincula esta estrategia con la expansión regulatoria en EE. UU. y con inversiones significativas de ICE, sugiriendo que parte de la profundidad observada en mercados de alto perfil podría provenir de esta mesa interna.[33]

### 8.2. Gobernanza, oráculos y confianza

Diversas fuentes (CoinMarketCap, Coinglass, Binance Square) documentan controversias de gobernanza ligadas a la resolución de mercados, como el caso de la apuesta sobre un acuerdo de minerales entre EE. UU. y Ucrania que se resolvió como “sí” pese a no existir acuerdo firmado, atribuida a la influencia de un gran holder de UMA en el oráculo. Polymarket respondió en Discord calificando el caso de “sin precedentes” y prometiendo mejoras de proceso, pero manteniendo que no se trató de un fallo de mercado a efectos de reembolso.[34][35][36]

Aunque estos incidentes afectan a la percepción de riesgo de evento, no hay indicios de que hayan modificado la estructura del CLOB: los cambios se centran más en reglas de resolución y relaciones con el oráculo UMA que en la microestructura del libro de órdenes.[35][34]

### 8.3. Integraciones y front‑ends externos

MetaMask Predictions, Polyman, Wangr y otros front‑ends independientes consumen el order book de Polymarket vía API y websockets para mostrar depth real, simular fills y ofrecer paper trading o análisis de costes. Estas integraciones tienden a concentrarse en los mercados más líquidos (sports, política, macro) y refuerzan aún más la priorización de esos libros frente al resto del universo de mercados listados.[27][37][9]

## 9. Resumen: ¿ha cambiado estructuralmente el CLOB y qué significa para market making puro en 2026?

En términos de arquitectura, el CLOB de Polymarket no ha sido reemplazado por AMM ni por otro modelo; al contrario, ha sido reforzado con CLOB v2, mejores APIs y un enfoque claro en order book centralizado con settlement on‑chain. Los cambios de 2025–2026 son microestructurales (nuevos tipos de orden, delays específicos, rate limits, heartbeat, matching‑engine restarts) y de incentivos (fees dinámicos, maker rebates generalizados y grandes pools de liquidity rewards), pero el núcleo sigue siendo un libro de órdenes off‑chain con contratos CTF on‑chain.[2][16][5][1][14][4]

Lo que sí ha cambiado de forma profunda es **dónde** hay liquidez real y dónde operan los market makers: los datos muestran que la liquidez está extremadamente concentrada en unos pocos mercados de sports de alta frecuencia, grandes eventos políticos/macroeconómicos y algunos mercados de cripto de corta duración con rebates activos, mientras que la mayoría de mercados de corto plazo y muchos outrights deportivos presentan poco o ningún volumen en 24h y spreads muy amplios. La combinación de programas de maker rebates y liquidity rewards fuertemente segmentados hace que el retorno esperado de estrategias de market making puras dependa en gran medida de operar precisamente en esos mercados incentivados, con quoting competitivo cerca del midpoint y participación suficiente en volumen ejecutado para capturar rebates.[38][20][30][18][28][9]

En consecuencia, una estrategia de market making “puro spread capture” aplicada de forma indiscriminada a todo el universo de mercados de Polymarket probablemente se encontrará con un panorama similar al que describes: cientos de mercados con libros casi vacíos, bids ínfimos (0.001) y asks en 0.999, profundidad negligible y pocas oportunidades de capturar spreads pequeños y repetibles. En cambio, las evidencias apuntan a que sigue habiendo spreads de 1–5 centavos y market makers activos en un subconjunto relativamente pequeño de mercados muy líquidos (sports con rewards activos, elecciones y macro, algunos cripto de alta frecuencia), y que cualquier estrategia de market making rentable en 2026 tendrá que concentrarse deliberadamente en esos nichos, modelar el impacto de rebates y adaptarse a las nuevas condiciones de fees y delays.[28][9]