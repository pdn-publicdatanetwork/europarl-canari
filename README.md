# europarl-canari

Canari quotidien : le WAF AWS d'europarl.europa.eu (blocage HTTP 202 des IP datacenter) tolerate-t-il des requetes lentes emises depuis les runners GitHub Actions ?

## Principe

- 1 run par jour (cron 03:41 UTC) + declenchement manuel (`workflow_dispatch`).
- Liste des deputes europeens francais via l'API open data v2 du Parlement europeen (CC BY 4.0).
- 1 requete HTTP par fiche, pause de 15 s, en-tetes navigateur. ~21 min pour 84 fiches.
- On ne releve **que des codes HTTP** : aucune donnee extraite, aucune page conservee.
- Resume du run dans le job summary + artifact `canari-resultats` (CSV).
- Le job echoue (notification) uniquement si **0 % de HTTP 200**.

## Interpretation

| Resultat | Conclusion |
|---|---|
| Fort taux de 200 | Le WAF ne bloque pas les IP Azure : la collecte peut tourner 100 % sur GitHub Actions, sans runner auto-heberge |
| 202 systematiques | Blocage confirme : maintien de la strategie actuelle + relance de la demande open data au Parlement |
| Resultats mixtes | Affiner (cadence, User-Agent) avant de conclure |

## Contexte projet

Projet DataParl' (dataparl.fr). Les noms des assistants des deputes ne sont publies nulle part en open data ; une demande officielle de publication d'un dataset est en parallele. Ce canari mesure la faisabilite d'une collecte quotidienne sans infrastructure personnelle.

## Conventions

- Commits : DataParl <support@dataparl.fr>
- Licence : MIT - Copyright (c) 2026 DataParl'
- Aucune donnee personnelle, aucun nom personnel dans ce depot.
