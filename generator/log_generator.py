#!/usr/bin/env python3
"""Simulateur de logs pour une plateforme e-commerce fictive ("ShopNow").

Chaque ligne écrite est un objet JSON. Le simulateur fait vivre des sessions
utilisateurs (navigation -> panier -> login -> commande -> paiement -> expédition)
et injecte périodiquement des scénarios réalistes :
  - incidents : passerelle de paiement dégradée, base de données lente
  - attaques par force brute sur /api/auth/login
  - erreurs serveur aléatoires, paniers abandonnés, stock faible

Uniquement la bibliothèque standard Python.
"""

import argparse
import json
import logging
import math
import os
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

# --------------------------------------------------------------------------- #
# Données de référence
# --------------------------------------------------------------------------- #

CATALOG = [
    {"sku": "SKU-1001", "name": "Smartphone Nova X12", "category": "electronics", "price": 699.00},
    {"sku": "SKU-1002", "name": "Casque sans fil Pulse", "category": "electronics", "price": 149.90},
    {"sku": "SKU-1003", "name": "Laptop UltraBook 14", "category": "electronics", "price": 1199.00},
    {"sku": "SKU-1004", "name": "Montre connectée Fit 3", "category": "electronics", "price": 229.00},
    {"sku": "SKU-2001", "name": "Sneakers Urban Run", "category": "fashion", "price": 89.99},
    {"sku": "SKU-2002", "name": "Veste en jean", "category": "fashion", "price": 59.90},
    {"sku": "SKU-2003", "name": "Sac à dos Explorer", "category": "fashion", "price": 45.00},
    {"sku": "SKU-3001", "name": "Machine à café Barista", "category": "home", "price": 329.00},
    {"sku": "SKU-3002", "name": "Aspirateur robot Clean+", "category": "home", "price": 279.00},
    {"sku": "SKU-3003", "name": "Set de casseroles Inox", "category": "home", "price": 119.00},
    {"sku": "SKU-4001", "name": "Roman - Le dernier voyage", "category": "books", "price": 18.50},
    {"sku": "SKU-4002", "name": "Guide Python avancé", "category": "books", "price": 42.00},
    {"sku": "SKU-5001", "name": "Tapis de yoga Pro", "category": "sports", "price": 34.90},
    {"sku": "SKU-5002", "name": "Vélo d'appartement Cardio", "category": "sports", "price": 449.00},
    {"sku": "SKU-5003", "name": "Haltères réglables 20kg", "category": "sports", "price": 99.00},
]
CATEGORIES = sorted({p["category"] for p in CATALOG})
# Les produits en début de liste sont plus populaires
PRODUCT_WEIGHTS = [1.0 / math.sqrt(i + 1) for i in range(len(CATALOG))]

SEARCH_TERMS = ["iphone", "casque", "laptop", "sneakers", "cafe", "aspirateur",
                "yoga", "velo", "montre", "sac", "livre python", "promo", "soldes"]

# Plages d'IP publiques par pays (1er octet, 2e octet min, 2e octet max), avec poids
IP_RANGES = [
    ("FR", (90, 0, 127), 30),     # Orange France
    ("TN", (41, 224, 231), 15),   # Tunisie Telecom
    ("DE", (79, 192, 255), 10),   # Deutsche Telekom
    ("US", (73, 0, 255), 12),     # Comcast
    ("GB", (86, 128, 191), 8),    # BT
    ("ES", (83, 32, 63), 7),      # Telefonica
    ("CA", (99, 224, 255), 5),    # Rogers
    ("MA", (105, 154, 159), 8),   # Maroc Telecom
]
ATTACKER_IPS = ["185.220.101.34", "45.155.205.233", "193.142.146.10", "91.240.118.172"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]
ATTACKER_USER_AGENTS = ["python-requests/2.31.0", "curl/8.4.0", "Hydra/9.5"]
BRUTEFORCE_USERNAMES = ["admin", "root", "test", "administrator", "support", "user"]

PAYMENT_METHODS = [("card", 70), ("paypal", 18), ("apple_pay", 8), ("bank_transfer", 4)]
DECLINE_REASONS = ["insufficient_funds", "card_expired", "fraud_suspected", "3ds_authentication_failed"]
CARRIERS = ["colissimo", "chronopost", "dhl", "ups"]

SERVICES = {
    # service: (nombre d'instances, latence de base en ms)
    "frontend": (3, 40),
    "catalog": (2, 60),
    "search": (2, 90),
    "cart": (2, 50),
    "auth": (2, 120),
    "order": (2, 150),
    "payment": (2, 400),
    "shipping": (1, 100),
    "inventory": (1, 30),
}

SERVER_ERRORS = {
    500: [("InternalServerError", "Unexpected error while processing request"),
          ("NullPointerException", "Cannot invoke method on null object reference"),
          ("DatabaseError", "Deadlock found when trying to get lock; try restarting transaction")],
    502: [("BadGateway", "Invalid response from upstream service")],
    503: [("ServiceUnavailable", "Service temporarily unavailable, circuit breaker open")],
    504: [("GatewayTimeout", "Upstream request timed out")],
}


def weighted_choice(pairs):
    items, weights = zip(*pairs)
    return random.choices(items, weights=weights, k=1)[0]


def random_client_ip():
    country, (a, b_min, b_max), _ = random.choices(IP_RANGES, weights=[r[2] for r in IP_RANGES], k=1)[0]
    return f"{a}.{random.randint(b_min, b_max)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def short_id():
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------- #
# Session utilisateur
# --------------------------------------------------------------------------- #

class Session:
    def __init__(self):
        self.session_id = short_id()
        self.client_ip = random_client_ip()
        self.user_agent = random.choice(USER_AGENTS)
        # 60 % des visiteurs ont un compte
        self.account = f"user_{random.randint(1000, 9999)}" if random.random() < 0.6 else None
        self.user_id = None  # renseigné après login
        self.cart = []       # liste de (produit, quantité)
        self.steps = 0
        self.max_steps = random.randint(3, 25)
        self.started = False

    @property
    def cart_total(self):
        return round(sum(p["price"] * q for p, q in self.cart), 2)


# --------------------------------------------------------------------------- #
# Générateur
# --------------------------------------------------------------------------- #

class Generator:
    def __init__(self, emit, target_sessions=40, incident_interval=900, attack_interval=600):
        self.emit = emit
        self.target_sessions = target_sessions
        self.incident_interval = incident_interval
        self.attack_interval = attack_interval
        self.sessions = []
        self.clock = time.time()
        self._offset = 0.0
        self.incident = None          # (type, fin)
        self.next_incident_at = None
        self.attack = None            # dict ip / ua / restant
        self.next_attack_at = None
        self.lines = 0

    # ---- écriture -------------------------------------------------------- #

    def log(self, level, service, event, message, **fields):
        # Plusieurs lignes d'un même tick : on les espace de quelques ms
        self._offset += random.uniform(0.001, 0.05)
        ts = datetime.fromtimestamp(self.clock + self._offset, tz=timezone.utc)
        instances = SERVICES[service][0]
        record = {
            "timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": level,
            "service": service,
            "server": f"{service}-{random.randint(1, instances)}",
            "event": event,
            "message": message,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        self.emit(json.dumps(record, ensure_ascii=False))
        self.lines += 1

    def session_fields(self, s):
        return {"session_id": s.session_id, "user_id": s.user_id,
                "client_ip": s.client_ip, "user_agent": s.user_agent}

    # ---- latence / erreurs ---------------------------------------------- #

    def incident_active(self, kind):
        return self.incident is not None and self.incident[0] == kind

    def latency(self, service):
        ms = SERVICES[service][1] * random.lognormvariate(0, 0.45)
        if self.incident_active("database_slow") and service in ("catalog", "cart", "order", "inventory"):
            ms *= random.uniform(6, 15)
        if self.incident_active("payment_gateway_degraded") and service == "payment":
            ms += random.uniform(2500, 9000)
        return int(ms)

    def server_error_rate(self, service):
        if self.incident_active("payment_gateway_degraded") and service == "payment":
            return 0.35
        if self.incident_active("database_slow") and service in ("catalog", "cart", "order", "inventory"):
            return 0.06
        return 0.006

    def request(self, s, service, method, path, status=200, trace_id=None, **extra):
        """Log d'accès HTTP. Retourne le code HTTP effectif (peut devenir 5xx)."""
        rt = self.latency(service)
        error_type = error_message = None
        if status < 400 and random.random() < self.server_error_rate(service):
            if service == "payment" and self.incident_active("payment_gateway_degraded"):
                status = random.choice([502, 504, 504])
            else:
                status = random.choice([500, 500, 500, 502, 503])
            error_type, error_message = random.choice(SERVER_ERRORS[status])
        if status >= 500:
            level = "ERROR"
        elif status >= 400 or rt > 2000:
            level = "WARN"
        else:
            level = "INFO"
        self.log(level, service, "http.request", f"{method} {path} -> {status} ({rt} ms)",
                 trace_id=trace_id, http_method=method, url_path=path,
                 status_code=status, response_time_ms=rt,
                 error_type=error_type, error_message=error_message,
                 **self.session_fields(s), **extra)
        return status

    # ---- parcours utilisateur ------------------------------------------- #

    def step_session(self, s):
        s.steps += 1
        if not s.started:
            s.started = True
            self.request(s, "frontend", "GET", "/")
            return
        if s.steps > s.max_steps:
            self.end_session(s)
            return
        if s.cart and random.random() < 0.3:
            self.checkout(s)
            self.end_session(s)
            return

        r = random.random()
        if r < 0.25:
            self.search(s)
        elif r < 0.5:
            cat = random.choice(CATEGORIES)
            self.request(s, "catalog", "GET", f"/api/products?category={cat}", category=cat)
        elif r < 0.95:
            self.view_product(s)
        else:
            self.end_session(s)

    def search(self, s):
        q = random.choice(SEARCH_TERMS)
        st = self.request(s, "search", "GET", f"/api/search?q={q.replace(' ', '+')}")
        if st == 200:
            n = random.choice([0] + [random.randint(1, 120)] * 6)
            self.log("INFO", "search", "search.performed", f"Recherche '{q}' : {n} résultats",
                     search_query=q, results_count=n, **self.session_fields(s))

    def view_product(self, s):
        p = random.choices(CATALOG, weights=PRODUCT_WEIGHTS, k=1)[0]
        st = self.request(s, "catalog", "GET", f"/api/products/{p['sku']}",
                          product_sku=p["sku"], category=p["category"])
        if st == 200 and random.random() < 0.3:
            qty = random.choices([1, 2, 3], weights=[80, 15, 5])[0]
            if self.request(s, "cart", "POST", "/api/cart/items", product_sku=p["sku"]) != 200:
                return
            s.cart.append((p, qty))
            self.log("INFO", "cart", "cart.item_added",
                     f"{qty} x {p['name']} ajouté au panier",
                     product_sku=p["sku"], product_name=p["name"], category=p["category"],
                     quantity=qty, amount=round(p["price"] * qty, 2), currency="EUR",
                     **self.session_fields(s))

    def login(self, s, trace_id):
        for attempt in (1, 2):
            ok = random.random() > 0.08
            st = self.request(s, "auth", "POST", "/api/auth/login", status=200 if ok else 401,
                              trace_id=trace_id)
            if st == 200:
                s.user_id = s.account or f"user_{random.randint(10000, 99999)}"
                self.log("INFO", "auth", "user.login", f"Connexion réussie pour {s.user_id}",
                         trace_id=trace_id, login_status="success", username=s.user_id,
                         **self.session_fields(s))
                return True
            self.log("WARN", "auth", "user.login", "Échec de connexion : identifiants invalides",
                     trace_id=trace_id, login_status="failed",
                     username=s.account or "guest", attempt=attempt,
                     **self.session_fields(s))
            if random.random() < 0.5:
                break
        return False

    def checkout(self, s):
        trace_id = uuid.uuid4().hex
        if s.user_id is None and not self.login(s, trace_id):
            return

        if self.request(s, "order", "POST", "/api/checkout", trace_id=trace_id) != 200:
            return
        order_id = f"ORD-{datetime.fromtimestamp(self.clock, tz=timezone.utc):%Y%m%d}-{random.randint(100000, 999999)}"
        total = s.cart_total
        items = sum(q for _, q in s.cart)
        self.log("INFO", "order", "order.created", f"Commande {order_id} créée ({items} articles, {total} EUR)",
                 trace_id=trace_id, order_id=order_id, quantity=items, amount=total,
                 currency="EUR", **self.session_fields(s))

        method = weighted_choice(PAYMENT_METHODS)
        declined = random.random() < 0.06
        st = self.request(s, "payment", "POST", "/api/payments", status=402 if declined else 200,
                          trace_id=trace_id, order_id=order_id, payment_method=method)
        common = dict(trace_id=trace_id, order_id=order_id, amount=total, currency="EUR",
                      payment_method=method, **self.session_fields(s))
        if st >= 500:
            self.log("ERROR", "payment", "payment.failed",
                     f"Paiement {order_id} en erreur : fournisseur de paiement indisponible",
                     payment_status="error", **common)
            return
        if declined:
            reason = random.choice(DECLINE_REASONS)
            self.log("WARN", "payment", "payment.declined", f"Paiement {order_id} refusé : {reason}",
                     payment_status="declined", decline_reason=reason, **common)
            return

        self.log("INFO", "payment", "payment.succeeded", f"Paiement {order_id} accepté ({total} EUR)",
                 payment_status="success", **common)
        for p, q in s.cart:
            self.log("INFO", "inventory", "inventory.reserved", f"Stock réservé : {q} x {p['sku']}",
                     trace_id=trace_id, order_id=order_id, product_sku=p["sku"], quantity=q)
        carrier = random.choice(CARRIERS)
        self.log("INFO", "shipping", "shipment.created", f"Expédition créée pour {order_id} via {carrier}",
                 trace_id=trace_id, order_id=order_id, carrier=carrier, user_id=s.user_id)
        s.cart = []

    def end_session(self, s):
        if s.cart:
            self.log("INFO", "cart", "cart.abandoned",
                     f"Panier abandonné ({len(s.cart)} produits, {s.cart_total} EUR)",
                     quantity=sum(q for _, q in s.cart), amount=s.cart_total, currency="EUR",
                     **self.session_fields(s))
        if s in self.sessions:
            self.sessions.remove(s)

    # ---- scénarios de fond ---------------------------------------------- #

    def update_incidents(self):
        if self.next_incident_at is None:
            self.next_incident_at = self.clock + random.uniform(0.3, 1.0) * self.incident_interval
        if self.incident and self.clock >= self.incident[1]:
            kind = self.incident[0]
            service = "payment" if kind == "payment_gateway_degraded" else "order"
            self.log("INFO", service, "incident.resolved", f"Incident résolu : {kind}", incident=kind)
            self.incident = None
        if self.incident is None and self.clock >= self.next_incident_at:
            kind = random.choice(["payment_gateway_degraded", "database_slow"])
            duration = random.uniform(60, 240)
            self.incident = (kind, self.clock + duration)
            self.next_incident_at = self.clock + duration + random.uniform(0.5, 1.5) * self.incident_interval
        if self.incident_active("database_slow") and random.random() < 0.1:
            ms = random.randint(2000, 12000)
            self.log("WARN", random.choice(["catalog", "order", "cart"]), "db.slow_query",
                     f"Requête SQL lente ({ms} ms)", query_duration_ms=ms,
                     query=random.choice(["SELECT * FROM products WHERE category = ?",
                                          "UPDATE carts SET updated_at = ? WHERE id = ?",
                                          "INSERT INTO orders (...) VALUES (...)"]))

    def update_attack(self):
        if self.next_attack_at is None:
            self.next_attack_at = self.clock + random.uniform(0.3, 1.0) * self.attack_interval
        if self.attack is None and self.clock >= self.next_attack_at:
            self.attack = {"ip": random.choice(ATTACKER_IPS), "ua": random.choice(ATTACKER_USER_AGENTS),
                           "remaining": random.randint(40, 120)}
            self.next_attack_at = self.clock + random.uniform(0.5, 1.5) * self.attack_interval
        if self.attack:
            for _ in range(random.randint(1, 3)):
                username = random.choice(BRUTEFORCE_USERNAMES)
                rt = self.latency("auth")
                self.log("WARN", "auth", "user.login", f"Échec de connexion pour '{username}'",
                         login_status="failed", username=username, client_ip=self.attack["ip"],
                         user_agent=self.attack["ua"], http_method="POST", url_path="/api/auth/login",
                         status_code=401, response_time_ms=rt)
                self.attack["remaining"] -= 1
            if self.attack["remaining"] <= 0:
                self.log("WARN", "auth", "security.ip_blocked",
                         f"IP {self.attack['ip']} bloquée après trop d'échecs de connexion",
                         client_ip=self.attack["ip"])
                self.attack = None

    def background_noise(self):
        if random.random() < 0.01:
            p = random.choice(CATALOG)
            left = random.randint(0, 5)
            self.log("WARN", "inventory", "inventory.low_stock", f"Stock faible pour {p['sku']} : {left} restants",
                     product_sku=p["sku"], product_name=p["name"], stock_level=left)

    # ---- boucle --------------------------------------------------------- #

    def tick(self):
        self._offset = 0.0
        self.update_incidents()
        self.update_attack()
        self.background_noise()
        if len(self.sessions) < self.target_sessions and random.random() < 0.6:
            self.sessions.append(Session())
        if self.sessions:
            self.step_session(random.choice(self.sessions))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_emitter(log_file, to_stdout):
    logger = logging.getLogger("ecommerce")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=200 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    def emit(line):
        logger.info(line)
        if to_stdout:
            print(line, flush=True)
    return emit


def main():
    parser = argparse.ArgumentParser(description="Simulateur de logs e-commerce")
    parser.add_argument("--rate", type=float, default=float(os.getenv("GENERATOR_RATE", 5)),
                        help="actions utilisateur par seconde (défaut 5)")
    parser.add_argument("--backfill-hours", type=float, default=float(os.getenv("GENERATOR_BACKFILL_HOURS", 0)),
                        help="heures d'historique à générer instantanément au démarrage")
    parser.add_argument("--log-file", default=os.getenv("LOG_FILE", "logs/ecommerce.log"))
    parser.add_argument("--stdout", action="store_true", help="afficher aussi les logs sur la sortie standard")
    parser.add_argument("--duration", type=float, default=0, help="arrêt après N secondes (0 = infini)")
    parser.add_argument("--seed", type=int, help="graine aléatoire (reproductibilité)")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    running = True

    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    gen = Generator(build_emitter(args.log_file, args.stdout))
    status = sys.stderr if args.stdout else sys.stdout

    # Historique : horloge virtuelle qui avance sans attendre jusqu'à maintenant
    if args.backfill_hours > 0:
        gen.clock = time.time() - args.backfill_hours * 3600
        print(f"Backfill de {args.backfill_hours} h d'historique...", file=status, flush=True)
        while running and gen.clock < time.time():
            gen.tick()
            gen.clock += random.expovariate(args.rate)
        print(f"Backfill terminé : {gen.lines} lignes", file=status, flush=True)

    print(f"Génération en temps réel : {args.rate} actions/s -> {args.log_file}", file=status, flush=True)
    start = last_report = time.time()
    while running:
        time.sleep(random.expovariate(args.rate))
        gen.clock = time.time()
        gen.tick()
        if gen.clock - last_report >= 30:
            incident = gen.incident[0] if gen.incident else "aucun"
            print(f"[stats] lignes={gen.lines} sessions={len(gen.sessions)} incident={incident} "
                  f"attaque={'oui' if gen.attack else 'non'}", file=status, flush=True)
            last_report = gen.clock
        if args.duration and gen.clock - start >= args.duration:
            break


if __name__ == "__main__":
    main()
