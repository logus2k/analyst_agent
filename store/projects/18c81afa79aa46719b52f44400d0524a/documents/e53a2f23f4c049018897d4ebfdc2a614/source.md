# Task : Share

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `09-documents`

## Sequência de execução

1. Domínio: link temporário, token (nanoid), expiração de 7 dias, reativação, erros — com testes
2. Migration: tabela de links temporários
3. Repositório: persistência e lookup de links por prescrição
4. Use cases: gerar link temporário, reativar link expirado, download via token
5. Handler: endpoints de share
   - `POST /prescriptions/:id/share` — `doctor`
   - `PATCH /prescriptions/:id/share/reactivate` — `doctor`
   - `GET /prescriptions/download/:token` — público (token temporário no link)
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Cache: onde aplicável (ex: lookup de token)

## Restrições

- Link só pode ser gerado para prescrições com PDF já gerado
- Link expira em 7 dias
- Download não requer autenticação — apenas token válido
- Doctor pode reativar link expirado
- Módulo não muta conteúdo clínico da prescrição

## Progresso

- [x] Domínio — 2026-06-25: ShareLink, New, IsExpired, Reactivate, erros, testes
- [x] Migration — 2026-06-25: 000009_create_share_links (UNIQUE por prescrição)
- [x] Repositório — 2026-06-25: Save, FindByToken, FindByPrescriptionPublicID, Update, HasDocumentForDoctor, FindPDFByPrescriptionPublicID
- [x] Use cases — 2026-06-25: CreateShareLink, ReactivateShareLink, DownloadByToken
- [x] Handler — 2026-06-25: POST /prescriptions/:id/share, PATCH /prescriptions/:id/share/reactivate, GET /prescriptions/download/:token com Swagger
- [x] Cache — 2026-06-25: FindByToken com Redis (TTL = min(5min, tempo restante do link))
- [x] Arquivo .http — 2026-06-25: references/http/10-share.http

## Critério de conclusão

- Doctor consegue gerar link temporário
- Patient consegue fazer download via link válido
- Link expirado retorna erro apropriado
- Doctor consegue reativar link expirado
- Domínio testado
