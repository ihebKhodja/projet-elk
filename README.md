# ELK Stack – Logs d'une plateforme e-commerce

Stack ELK (Elasticsearch, Logstash, Kibana + Filebeat) sous Docker, alimentée par un simulateur
de logs d'une boutique en ligne fictive, **ShopNow**.

```
┌───────────────┐   JSON   ┌──────────┐  5044  ┌──────────┐       ┌───────────────┐      ┌────────┐
│ log-generator │ ───────► │ Filebeat │ ─────► │ Logstash │ ────► │ Elasticsearch │ ◄─── │ Kibana │
│   (Python)    │  logs/   └──────────┘        │ parsing  │       │ ecommerce-    │      │  5601  │
└───────────────┘                              │ GeoIP/UA │       │ logs-YYYY.MM.dd      └────────┘
                                               └──────────┘       └───────────────┘
```

## Démarrage

```bash
docker compose up -d --build
```

| Service       | URL                    |
|---------------|------------------------|
| Kibana        | http://localhost:5601  |
| Elasticsearch | http://localhost:9200  |

Au démarrage, le conteneur `setup` crée le template d'index et la data view Kibana
**E-commerce logs**. Le simulateur génère ensuite 2 h d'historique, puis des logs en continu.
Dans Kibana, ouvrir **Discover** avec la data view `E-commerce logs`.

Arrêt : `docker compose down` (ajouter `-v` pour supprimer aussi les données).

> Prérequis : ~4 Go de RAM libres et `vm.max_map_count >= 262144`
> (`sudo sysctl -w vm.max_map_count=262144` si Elasticsearch ne démarre pas).

## Structure

```
.
├── docker-compose.yml
├── .env                          # version de la stack, mémoire, débit du simulateur
├── generator/
│   ├── Dockerfile
│   └── log_generator.py          # simulateur (stdlib Python uniquement)
├── filebeat/filebeat.yml         # lit logs/ecommerce.log -> Logstash
├── logstash/
│   ├── config/logstash.yml
│   └── pipeline/ecommerce.conf   # json, date, geoip, useragent, tags
├── setup/
│   ├── index-template.json       # mapping (geo_point, ip, nombres, keywords)
│   └── setup.sh                  # template + data view Kibana
└── logs/                         # fichier de logs partagé
```

## Le simulateur

Il fait vivre des sessions utilisateurs : navigation → recherche → fiche produit → panier →
login → commande → paiement → réservation de stock → expédition. Et il injecte des scénarios :

| Scénario                     | Ce qu'on voit dans les logs                                      |
|------------------------------|------------------------------------------------------------------|
| Passerelle de paiement lente | `payment` : latence 2,5–9 s, ~35 % de 502/504, `payment.failed`  |
| Base de données lente        | latences ×6–15 sur catalog/cart/order, `db.slow_query`           |
| Attaque par force brute      | rafale de `user.login` en échec depuis une IP, puis `security.ip_blocked` |
| Paiement refusé              | `payment.declined` avec `decline_reason`                         |
| Panier abandonné             | `cart.abandoned` avec le montant perdu                           |
| Bruit de fond                | erreurs 5xx aléatoires, `inventory.low_stock`                    |

Exemple de ligne :

```json
{"timestamp": "2026-10-27T09:43:55.262Z", "level": "INFO", "service": "order", "server": "order-1",
 "event": "order.created", "message": "Commande ORD-20261027-482913 créée (2 articles, 788.99 EUR)",
 "trace_id": "c55acc671b6e48338e37348add2825e4", "order_id": "ORD-20261027-482913", "amount": 788.99,
 "currency": "EUR", "session_id": "9d2020669c084cf5", "user_id": "user_3337", "client_ip": "90.49.40.95", ...}
```

Le `trace_id` relie toutes les lignes d'un même checkout (login, commande, paiement, expédition).

### Utilisation en local (sans Docker)

```bash
python3 generator/log_generator.py --rate 10 --stdout              # temps réel
python3 generator/log_generator.py --backfill-hours 6 --duration 1 # 6 h d'historique puis arrêt
```

Options : `--rate` (actions/s), `--backfill-hours`, `--log-file`, `--stdout`, `--duration`, `--seed`.
Dans Docker, le débit se règle dans `.env` (`GENERATOR_RATE`, `GENERATOR_BACKFILL_HOURS`).

## Champs principaux

| Champ | Description |
|---|---|
| `level`, `service`, `server`, `event` | niveau, microservice, instance, type d'événement |
| `http_method`, `url_path`, `status_code`, `response_time_ms` | log d'accès HTTP (`event: http.request`) |
| `session_id`, `user_id`, `client_ip`, `trace_id` | corrélation |
| `order_id`, `amount`, `currency`, `payment_method`, `payment_status` | commande / paiement |
| `product_sku`, `product_name`, `category`, `quantity` | produit |
| `error_type`, `error_message` | erreurs serveur |
| `geoip.*` (ajouté par Logstash) | pays, ville, `geoip.location` (geo_point) |
| `ua.*` (ajouté par Logstash) | navigateur, OS, device |
| `tags` | `slow_request` (> 1 s), `server_error` (5xx) |

## Requêtes KQL utiles (Discover)

```
level : "ERROR"
service : "payment" and status_code >= 500
event : "payment.succeeded"
event : "user.login" and login_status : "failed"
tags : "slow_request"
event : "db.slow_query"
trace_id : "<un trace_id>"                 # suivre un checkout de bout en bout
geoip.country_code2 : "TN" and event : "order.created"
```

## Idées de dashboard (Kibana → Dashboards → Create)

- **Chiffre d'affaires** : somme de `amount` où `event : payment.succeeded`, par intervalle de temps
- **Taux d'erreur** : nombre de documents par `level` dans le temps (barres empilées)
- **Latence** : percentile 95 de `response_time_ms` ventilé par `service`
- **Funnel** : comptes de `cart.item_added` / `order.created` / `payment.succeeded`
- **Carte** : Maps → couche Documents sur `geoip.location`
- **Sécurité** : top `client_ip` pour `login_status : failed`
- **Moyens de paiement / refus** : camembert sur `payment_method`, puis sur `decline_reason`

## Dépannage

```bash
docker compose ps                     # état et healthchecks
docker compose logs -f logstash       # erreurs de parsing
docker compose logs -f log-generator  # statistiques du simulateur
curl "localhost:9200/_cat/indices/ecommerce-*?v"
```

Pour voir les événements tels qu'ils sortent de Logstash, décommenter la sortie
`stdout { codec => rubydebug }` dans `logstash/pipeline/ecommerce.conf` puis
`docker compose restart logstash`.
