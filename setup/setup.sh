#!/bin/sh
set -e
ES=http://elasticsearch:9200
KB=http://kibana:5601

echo "==> Création du template d'index ecommerce-logs"
curl -sf -X PUT "$ES/_index_template/ecommerce-logs" \
  -H 'Content-Type: application/json' \
  --data-binary @/setup/index-template.json
echo

echo "==> Création de la data view Kibana"
curl -sf -X POST "$KB/api/data_views/data_view" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  -d '{
        "override": true,
        "data_view": {
          "id": "ecommerce-logs",
          "title": "ecommerce-logs-*",
          "name": "E-commerce logs",
          "timeFieldName": "@timestamp"
        }
      }' > /dev/null

curl -sf -X POST "$KB/api/data_views/default" \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  -d '{"data_view_id": "ecommerce-logs", "force": true}' > /dev/null

echo "==> Setup terminé"
