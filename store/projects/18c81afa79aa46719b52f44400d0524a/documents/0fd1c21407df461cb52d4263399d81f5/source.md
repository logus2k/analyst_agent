# Task 12: Schedule

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Executar após:** `11-attendant-doctor`

## Sequência de execução

1. Domínio: slot recorrente, appointment, status, capacidade, encaixe, lifecycle, erros — com testes
2. Migration: tabelas de slots e appointments
3. Repositório: persistência de slots e appointments, lookup de disponibilidade
4. Use cases de slot: doctor cria slot recorrente, doctor edita slot, doctor desativa slot
5. Use cases de appointment: attendant agenda patient em data específica dentro da recorrência, attendant realoca appointment, attendant cancela appointment, doctor marca patient como atendido
6. Handler: endpoints de schedule
   - `POST /doctors/:id/slots` — `doctor`
   - `PUT /doctors/:id/slots/:slotId` — `doctor`
   - `PATCH /doctors/:id/slots/:slotId/deactivate` — `doctor`
   - `GET /doctors/:id/slots` — `doctor`, `attendant` (vinculado)
   - `POST /doctors/:id/slots/:slotId/appointments` — `attendant` (vinculado)
   - `PATCH /doctors/:id/slots/:slotId/appointments/:appointmentId/reschedule` — `attendant` (vinculado)
   - `PATCH /doctors/:id/slots/:slotId/appointments/:appointmentId/cancel` — `attendant` (vinculado)
   - `PATCH /doctors/:id/slots/:slotId/appointments/:appointmentId/attend` — `doctor`
   - `GET /doctors/:id/appointments` — `doctor`, `attendant` (vinculado)
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo
7. Cache: onde aplicável (ex: disponibilidade de slots)

## Restrições

- Attendant só gerencia agenda do doctor ao qual está vinculado
- Slot define recorrência (dia da semana + horário + capacidade) — não cria appointments automaticamente
- Attendant escolhe a data específica ao agendar dentro da recorrência do slot
- Slot tem capacidade definida pelo doctor
- Encaixe é permitido além da capacidade — o attendant decide explicitamente, o sistema não bloqueia
- Encaixe marcado explicitamente no appointment
- Status do appointment: `SCHEDULED`, `ATTENDED`, `CANCELLED`, `RESCHEDULED`
- Ao realocar, appointment original recebe status `RESCHEDULED` e fica como histórico — não aparece nas listagens mas é auditável
- Cancelamento usa status `CANCELLED` — sem hard delete
- Sem hard delete

## Progresso

- [x] Domínio — 2026-06-25: Slot (NewSlot, Deactivate, Update), Appointment (NewAppointment, Cancel, MarkRescheduled, MarkAttended), erros, testes
- [x] Migration — 2026-06-25: 000011_create_schedule (schedule_slots + schedule_appointments com índices)
- [x] Repositório — 2026-06-25: SaveSlot, FindSlotByPublicID, UpdateSlot, ListSlotsByDoctorUserID, SaveAppointment, FindAppointmentByPublicID, UpdateAppointment, ListAppointmentsByDoctorUserID, CountScheduledForSlotDate, GetDoctorUserIDByPublicID, IsAttendantLinkedToDoctor, FindUserNameByPublicID
- [x] Use cases de slot — 2026-06-25: CreateSlot, EditSlot, DeactivateSlot, ListSlots
- [x] Use cases de appointment — 2026-06-25: ScheduleAppointment, RescheduleAppointment (original → RESCHEDULED + novo SCHEDULED), CancelAppointment, AttendAppointment, ListAppointments (exclui RESCHEDULED)
- [x] Handler — 2026-06-25: todos os endpoints com Swagger
- [x] Cache — 2026-06-25: não aplicável (IsAttendantLinkedToDoctor lê tabela diretamente; dados de agenda mudam com frequência)
- [x] Arquivo .http — 2026-06-25: references/http/12-schedule.http

## Critério de conclusão

- Domínio e lifecycle testados
- Doctor consegue criar e gerenciar slots recorrentes
- Attendant vinculado consegue agendar, realocar e cancelar appointments
- Encaixe funcional além da capacidade do slot
- Doctor consegue marcar patient como atendido
- Histórico de realocações preservado
