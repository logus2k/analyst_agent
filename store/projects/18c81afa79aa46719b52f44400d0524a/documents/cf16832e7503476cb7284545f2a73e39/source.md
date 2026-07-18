# Task : Doctor

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `03-auth`

## Sequência de execução

1. Domínio: `Doctor`, atributo `crm`, `doctor_status`, ativação, elegibilidade, erros — com testes
2. Migration: tabela `doctors` (1x1 com `users`)
3. Repositório: persistência, lookup, listagem com paginação cursor-based cursor-based e filtro por nome
4. Use cases: completar perfil de médico (para users que registraram via OAuth), ativação/desativação pelo admin, listagem, lookup de elegibilidade
5. Handler: endpoints de doctor
   - `POST /doctors` — removido (registro feito via `POST /auth/register/doctor`)
   - `POST /doctors/complete` — autenticado (para users que registraram via OAuth, adiciona crm)
   - `GET /doctors` — `admin`
   - `PATCH /doctors/:id/activate` — `admin`
   - `PATCH /doctors/:id/deactivate` — `admin`
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
6. Cache: onde aplicável (ex: elegibilidade de doctor)

## Restrições

- Doctor tem tabela própria `doctors` — 1x1 com `users`
- Registro por password: `POST /auth/register/doctor` — cria User + AuthAccount + Doctor em uma transação
- Registro por OAuth: user completa perfil via `POST /doctors/complete` após autenticação
- Registro não ativa elegibilidade automaticamente
- Admin é quem ativa o perfil doctor
- `attendant` não tem acesso a doctors
- `crm` e `doctor_status` pertencem à tabela `doctors`
- Sem hard delete

## Progresso

- [x] Domínio — 2026-06-25
      Entidade `Doctor` (UserID, PublicUserID, Name, CRM, Status). `DoctorStatus` value object (pending/active/inactive). Activate, Deactivate com invariantes, IsEligible(). 9 testes cobrindo ciclo de vida e elegibilidade.
- [x] Migration — 2026-06-25
      Tabela `doctors` criada na migration 000003 (task 03, necessária para POST /auth/register/doctor). Nenhuma nova migration necessária.
- [x] Repositório — 2026-06-25
      infra/repository.go com JOIN users em todas as queries. Save() usa transação (UPDATE users SET role='doctor' + INSERT doctors). IsEligible() com cache Redis (key `doctor:eligible:{id}`, TTL 5min), invalidado no Update.
- [x] Use cases — 2026-06-25
      CompleteProfile (extrai userID do context), ActivateDoctor, DeactivateDoctor, ListDoctors, GetDoctor.
- [x] Handler — 2026-06-25
      POST /doctors/complete (autenticado), GET /doctors (admin), PATCH /doctors/{id}/activate (admin), PATCH /doctors/{id}/deactivate (admin). Swagger em cada handler.
- [x] Cache — 2026-06-25
      IsEligible() cacheado em Redis. Porta `EligibilityChecker` exposta em `Module.Eligibility()` para injeção em outros módulos (prescription, etc.).
- [x] Arquivo .http — 2026-06-25: references/http/04-doctor.http

## Critério de conclusão

- Domínio testado
- Fluxo de auto-registro e ativação pelo admin funcional
- Porta de elegibilidade disponível para outros módulos
