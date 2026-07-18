# Task : Attendant Doctor

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `03-auth`

## Sequência de execução

1. Domínio: vínculo attendant-doctor, lifecycle, erros — com testes
2. Migration: tabela de vínculos attendant-doctor
3. Repositório: persistência e lookup de vínculos
4. Use cases: doctor vincula attendant (gera token de convite e envia email), doctor desvincula attendant, lookup de vínculo
5. Handler: endpoints de attendant-doctor
   - `POST /doctors/:id/attendants/:attendantId` — `doctor`
   - `DELETE /doctors/:id/attendants/:attendantId` — `doctor`
   - `GET /doctors/:id/attendants` — `doctor`, `admin`
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Cache: onde aplicável (ex: lookup de vínculo)

## Restrições

- Apenas o doctor gerencia seus vínculos — sem aprovação de admin
- Ao vincular, sistema gera token de convite e envia email ao attendant
- Attendant usa o token para criar seu `AuthAccount` via senha ou OAuth
- Um attendant pode estar vinculado a mais de um doctor
- Sem hard delete — usar `deleted_at`
- Porta de lookup de vínculo disponível para o módulo schedule

## Progresso

- [x] Domínio — 2026-06-25: AttendantDoctor, New, Remove, IsRemoved, erros, testes
- [x] Migration — 2026-06-25: 000010_create_attendant_doctor_links (UNIQUE parcial WHERE deleted_at IS NULL)
- [x] Repositório — 2026-06-25: Save, FindByDoctorAndAttendant, Update, ListByDoctorPublicID, FindAttendantForLinking, IsLinked
- [x] Use cases — 2026-06-25: LinkAttendant (gera token + envia email), UnlinkAttendant, ListAttendants
- [x] Handler — 2026-06-25: POST /:id/attendants/:attendantId, DELETE /:id/attendants/:attendantId, GET /:id/attendants com Swagger
- [x] Cache — 2026-06-25: IsLinked pode ser consumido por schedule via leitura direta da tabela
- [x] Arquivo .http — 2026-06-25: references/http/11-attendant-doctor.http

## Critério de conclusão

- Domínio testado
- Doctor consegue vincular e desvincular attendant
- Porta de lookup disponível para o módulo schedule
