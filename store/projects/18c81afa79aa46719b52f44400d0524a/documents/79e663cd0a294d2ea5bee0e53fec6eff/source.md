# Software Design Document — Lootify

## Visão Geral

Lootify é uma aplicação de prescrição médica digital. Permite o cadastro de usuários, médicos, especialidades, medicamentos e a emissão de prescrições com assinatura digital ICP-Brasil.

---

## Atores

| Ator | Descrição |
|---|---|
| `admin` | Gestor do sistema. Ativa médicos, especialidades e medicamentos. |
| `attendant` | Cadastra usuários no sistema — tanto patients quanto staff. |
| `doctor` | Pode cadastrar patients quando não tem atendente vinculado. |

Primeiro `admin` criado via seed.
| `doctor` | Médico cadastrado e ativado. Emite prescrições. |
| `patient` | Todo usuário cadastrado é automaticamente um patient. |

---

## Módulos

### Identity

Todo usuário do sistema é um `User`.
Todo `User` é automaticamente um patient.
Nem todo `User` pode ser médico — isso requer cadastro específico e ativação pelo admin.

Usuários com login (`admin`, `attendant`, `doctor`) possuem um `AuthAccount`.
Patients não precisam de `AuthAccount`.

**Atributos de `User`:**
- `register` — identificador único do usuário no sistema

Todo `User` é automaticamente um patient.

**Roles com login:** `admin`, `attendant`, `doctor`.

**Login:**
- Password (email + senha)
- OAuth via `goth`

**Tokens:** access token + refresh token. Refresh tokens armazenados no banco.

**Regras:**
- `User.register` deve ser único.
- Médico pode se cadastrar, mas só faz login após ativação pelo admin.
- Patient não tem login por padrão.

---

### Patient

Capacidade de patient pertence a `User` — não é uma entidade separada.
Todo `User` cadastrado pelo attendant é automaticamente um patient.

**Regras:**
- Patient pode existir sem `AuthAccount`.
- Sem hard delete — lifecycle via status.

---

### Doctor

Módulo com tabela própria `doctors` — 1x1 com `users`.

**Atributos exclusivos (tabela `doctors`):**
- `crm` — registro do médico
- `doctor_status` — status de ativação

**Regras:**
- Registro por password: `POST /auth/register/doctor` — cria User + AuthAccount + Doctor em uma transação.
- Registro por OAuth: user completa perfil de médico via `POST /doctors/complete` após autenticação.
- Registro não ativa o perfil — admin deve ativar explicitamente.
- Auto-cadastro não ativa o perfil — admin deve ativar explicitamente.
- Apenas médicos ativos conseguem fazer login.
- Elegibilidade para prescrever requer ativação do perfil doctor + specialty aprovada.
- Sem hard delete.

---

### Specialty

Catálogo de especialidades médicas gerenciado pelo admin.

**Regras:**
- Apenas admin cadastra, edita e desativa specialties.
- Specialty nunca é deletada — apenas desativada.
- Médico solicita associação a uma specialty.
- Admin aprova ou rejeita a associação.
- Specialty aprovada é requisito para emitir prescrição.

---

### Doctor Specialty

Associação entre médico e specialty.

**Regras:**
- Médico solicita associação.
- Admin aprova.
- Elegibilidade para prescrever requer associação aprovada + perfil doctor ativo.
- Histórico de associações preservado — sem hard delete.

---

### Medication

Catálogo de medicamentos gerenciado pelo admin.

**Regras:**
- Apenas admin cadastra, edita e desativa medications.
- Medication nunca é deletada — apenas desativada.
- Atualização de medication não altera prescrições já emitidas.

---

### Prescription

Prescrição médica emitida por um doctor para um patient cadastrado no sistema.

**Estados:**
- `DRAFT` — rascunho, editável
- `ISSUED` — emitida, imutável
- `DISCARDED` — rascunho descartado
- `CANCELLED` — emitida e cancelada, requer motivo

**Regras:**
- Apenas `DRAFT` pode ser editado.
- Apenas doctors com perfil ativo e specialty aprovada podem emitir.
- Médico pode prescrever qualquer medicamento — sem restrição por specialty.
- Prescrição é para um patient cadastrado no sistema.
- Ao emitir, um snapshot do conteúdo clínico é gerado e armazenado — imutável.
- Cancelamento requer motivo.
- Registros cancelados e descartados permanecem auditáveis.
- Sem hard delete.

---

### Documents

Geração de PDF da prescrição emitida com assinatura digital ICP-Brasil.

**Fluxo:**
1. Médico emite a prescrição.
2. Médico solicita geração do PDF.
3. Sistema gera o PDF com o conteúdo do snapshot.
4. Sistema assina o PDF com a chave do médico (A1 ou A3).

**Regras:**
- PDF só pode ser gerado para prescrições no estado `ISSUED`.
- Conteúdo do PDF é baseado no snapshot — imutável.
- Médico deve ter chave A1 ou A3 cadastrada para assinar.
- Chaves ICP-Brasil são armazenadas no sistema.
- Padrão de assinatura: PAdES (ICP-Brasil).
- MVP assume baseline ICP-Brasil — sem geração ou verificação avançada de assinatura no MVP.

---

## Controle de Acesso

| Módulo | Ação | Roles |
|---|---|---|
| Identity | Registrar user | `attendant` |
| Identity | Ativar doctor | `admin` |
| Identity | Login | público |
| Patient | Listar/lookup | `admin`, `doctor` |
| Doctor | Auto-registro | autenticado |
| Doctor | Ativar/desativar | `admin` |
| Doctor | Listar | `admin` |
| Specialty | Cadastro/edição/desativação | `admin` |
| Specialty | Listagem | `admin`, `doctor` |
| Doctor Specialty | Solicitar associação | `doctor` |
| Doctor Specialty | Aprovar/rejeitar | `admin` |
| Medication | Cadastro/edição/desativação | `admin` |
| Medication | Listagem | `admin`, `doctor` |
| Prescription | Criar/editar/descartar/emitir/cancelar | `doctor` (próprias) |
| Prescription | Listar/lookup | `admin` (todas), `doctor` (próprias) |
| Documents | Cadastrar chave A1/A3 | `doctor` |
| Documents | Gerar PDF | `doctor` |
| Documents | Download via link temporário | público (token no link) |
| Documents | Reativar link | `doctor` |

`attendant` não tem acesso a doctors, medications ou prescrições no MVP.
Futuramente o attendant será vinculado a um doctor e terá acesso à agenda e patients do doctor vinculado.

## Link Temporário de Download

O doctor gera um link temporário para o patient baixar a prescrição em PDF.
O link expira por tempo.
O doctor pode reativar o link caso necessário.
O download é público — não requer autenticação, apenas o token válido no link.

---

## Attendant Doctor

Vínculo entre attendant e doctor.
O doctor gerencia seus próprios vínculos — sem aprovação de admin.
Um attendant pode estar vinculado a mais de um doctor.
O vínculo dá ao attendant acesso à agenda do doctor vinculado.

Ao vincular um attendant, o sistema gera um token de convite e envia por email.
O attendant usa o token para criar seu `AuthAccount` — escolhe entre senha ou OAuth Google.

## Schedule

Agenda de atendimento do doctor.

**Slots:**
- Doctor define slots recorrentes por dia da semana e horário
- Slot tem capacidade definida pelo doctor
- Encaixe é permitido além da capacidade — marcado explicitamente

**Appointments:**
- Attendant vinculado agenda patient em slot disponível
- Attendant vinculado pode cancelar appointment
- Attendant só gerencia agenda do doctor ao qual está vinculado
