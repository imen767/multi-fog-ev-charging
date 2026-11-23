# Multi-Fog — Préparation des deux PC de binômes (sans transfert de données)

Ce document peut être partagé aux binômes **dès maintenant**. Il ne contient aucun secret ni base SQLite.

## Répartition

| Machine | Services futurs | Port Fog |
|---|---|---|
| PC central | Fog A + Cloud Sync API + PostgreSQL + Chronos-2 + orchestrateur + dashboard + monitoring | 18001 |
| PC binôme B | Fog B + SQLite B + agent Fog B | 18002 |
| PC binôme C | Fog C + SQLite C + agent Fog C | 18003 |

## À préparer sur les PC B/C

1. Windows avec WSL2 Ubuntu et Docker Desktop configuré avec moteur WSL2 ; tester `wsl --status`, `docker version` et `docker compose version`.
2. Prévoir de l'espace disque pour les conteneurs et les catalogues. Vérifier que Docker démarre depuis Ubuntu WSL.
3. Les trois PC doivent se joindre sur un **même LAN privé / VPN** ; noter l'IPv4 du PC A, du PC B et du PC C (`ipconfig` sous Windows).
4. Sur un LAN privé seulement, le pare-feu doit autoriser l'accès entrant **depuis les autres PC du groupe** : `18002/TCP` sur B et `18003/TCP` sur C ; le PC central devra autoriser des accès ciblés aux services Cloud Sync (`18084/TCP`) et Chronos (`18085/TCP`). Le port Fog A est `18001/TCP`.
5. Ne jamais exposer ces API sur Internet public : les routes Fog de coopération n'ont pas encore d'authentification inter-Fog/TLS intégrée.
6. **Ne pas copier d'ancienne SQLite ou démarrer un Fog B/C vierge maintenant.** Le kit final viendra du PC central après gel et sauvegarde au tick courant.

## Le jour du transfert — ensemble avec le responsable du PC central

1. Vérifier l'état du cluster (`preflight.py`) et capturer une sauvegarde PostgreSQL.
2. Arrêter les agents B/C locaux et les conteneurs Fog B/C du PC central **sans supprimer leurs volumes**.
3. Générer les kits depuis les SQLite persistantes et leur checkpoint au même tick ; vérifier les empreintes SHA-256.
4. Sur chaque PC binôme, extraire uniquement son kit, remplir `.env.fog` avec les IP du LAN/VPN et le token de sync reçu via un canal privé, puis lancer `verify_fog_kit.py`.
5. Démarrer le Fog du binôme et son agent avec `compose.fog-node.yml`; tester `/health`, `/api/v1/round/status` et `/api/v1/piles/capacity`.
6. Depuis le PC central, contrôler les 3 Fog + l'orchestrateur avec `check_cluster.py` et comparer Fog/Cloud Sync.
7. **Ne déclencher un nouveau tick qu'après validation des trois Fog au même tick, `SETTLED` et aucun historique en attente.**

## Vérifications pour la soutenance

- Comparer le transfert **direct Fog B → Fog C** avec les appels HTTP et les états de réservation `HOLD → CONFIRM → ACTIVATE` ; un HOLD seul n'est pas une recharge réussie.
- Vérifier les erreurs d'un Fog indisponible et la reprise, puis distinguer clairement ces tests locaux de la future panne réelle sur trois machines.
- L'interface `Coopération Fog ↔ Fog` sur React utilise actuellement des mini-Fog isolés : elle **ne représente pas automatiquement les vrais échanges entre les PC B/C**. Pour montrer le cluster distribué réel, il faudra également recueillir une trace de ses communications sans la confondre avec la démo isolée.

**Règle absolue :** ne pas transmettre `.env.cloud`, les tokens Cloud, les dump PostgreSQL ou les SQLite à d'autres personnes que le destinataire autorisé.
