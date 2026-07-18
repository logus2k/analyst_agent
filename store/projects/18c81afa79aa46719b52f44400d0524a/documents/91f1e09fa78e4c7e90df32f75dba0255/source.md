# Task : Prescription

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `06-doctor-specialty`, `07-medication`

## Sequência de execução

1. Domínio: prescrição, item, lifecycle (`DRAFT`, `ISSUED`, descartada, cancelada), snapshot, erros — com testes
2. Migration: tabelas de prescrição e itens
3. Repositório: persistência, snapshot, listagem
4. Use cases de draft: criar, editar, descartar
5. Use cases de emissão: emitir com snapshot, cancelar com motivo
6. Use cases de consulta: listagem por doctor (próprias), listagem geral (admin), retrieve
7. Handler: endpoints de prescrição
   - `POST /prescriptions` — `doctor`
   - `PUT /prescriptions/:id` — `doctor` (próprias, apenas DRAFT)
   - `PATCH /prescriptions/:id/discard` — `doctor` (próprias)
   - `PATCH /prescriptions/:id/issue` — `doctor` (próprias)
   - `PATCH /prescriptions/:id/cancel` — `doctor` (próprias)
   - `GET /prescriptions` — `admin` (todas), `doctor` (próprias)
   - `GET /prescriptions/:id` — `admin`, `doctor` (próprias)
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
8. Cache: onde aplicável

## Restrições

- Apenas `DRAFT` é editável
- Conteúdo clínico emitido é imutável
- Emissão requer elegibilidade ativa do doctor e specialty aprovada
- Médico pode prescrever qualquer medicamento — sem restrição por specialty
- Prescrição é apenas para patients cadastrados no sistema
- `attendant` não tem acesso a prescrições
- Cancelamento requer motivo
- Sem hard delete

## Progresso

- [x] Domínio — 2026-06-25
      Entidades `Prescription` e `PrescriptionItem`. Lifecycle: DRAFT→ISSUED/DISCARDED, ISSUED→CANCELLED. `Issue()` exige itens. `Cancel()` exige motivo. 15 testes cobrindo todo o ciclo.
- [x] Migration — 2026-06-25
      `000007_create_prescriptions.up.sql`: tabelas `prescriptions` + `prescription_items` com índices em doctor_user_id, status e prescription_id.
- [x] Repositório — 2026-06-25
      `Issue()` faz snapshot de specialty_name e medication_name lendo tabelas specialties/medications diretamente. `Save()` resolve patient_user_id via users table. `FindByPublicID()` carrega itens.
- [x] Use cases de draft — 2026-06-25
      CreatePrescription, UpdatePrescription (replace all items), DiscardPrescription — todos verificam ownership via JWT claims.
- [x] Use cases de emissão — 2026-06-25
      IssuePrescription verifica doctor eligibility + specialty eligibility via portas injetadas antes de emitir. CancelPrescription exige motivo.
- [x] Use cases de consulta — 2026-06-25
      ListPrescriptions filtra por doctor_user_id automaticamente se role=doctor. GetPrescription verifica ownership para doctor.
- [x] Handler — 2026-06-25
      POST, PUT, PATCH/discard, PATCH/issue, PATCH/cancel, GET /, GET /{id}. Swagger em cada handler.
- [x] Cache — 2026-06-25
      Sem cache em prescrições (conteúdo clínico crítico, sempre lido do banco). Portas de elegibilidade já cacheadas nos módulos doctor e doctorspecialty.
- [x] Arquivo .http — 2026-06-25: references/http/08-prescription.http

## Critério de conclusão

- Domínio e lifecycle testados
- Snapshot estável após mudanças em medication e specialty
- Fluxos de draft, emissão e cancelamento funcionais
