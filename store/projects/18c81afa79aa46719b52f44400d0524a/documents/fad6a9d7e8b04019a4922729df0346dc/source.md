# Task : Documents

**Skill:** `go-conventions`
**Referência:** `references/sdd.md`
**Depende de:** `08-prescription`

## Sequência de execução

1. Domínio: chaves ICP-Brasil do médico (A1/A3), regras de armazenamento, erros — com testes
2. Migration: tabela de chaves do médico
3. Repositório: persistência e lookup de chaves por médico
4. Use cases: cadastro de chave A1/A3, geração de PDF, assinatura digital PAdES
5. Handler: endpoints de documents
   - `POST /doctors/:id/keys` — `doctor`
   - `POST /prescriptions/:id/pdf` — `doctor`
   - Documentar cada endpoint com anotações Swagger no mesmo arquivo do handler
   - Criar arquivo `.http` em `references/http/` com requests para todos os endpoints do módulo

## Restrições

- PDF só pode ser gerado para prescrições no estado `ISSUED`
- Conteúdo do PDF é baseado no snapshot — imutável
- Médico deve ter chave cadastrada para assinar
- Padrão de assinatura: PAdES (ICP-Brasil)
- Módulo não muta conteúdo clínico da prescrição

## Progresso

- [x] Domínio — 2026-06-25: DoctorKey (A1/A3), PrescriptionDocument, KeyType, erros, testes
- [x] Migration — 2026-06-25: 000008_create_documents (doctor_keys + prescription_documents)
- [x] Repositório — 2026-06-25: SaveKey, FindKeyByDoctorUserID, GenerateAndSave (PDF + signing baseline), FindDocumentByPrescriptionPublicID
- [x] Use cases — 2026-06-25: RegisterKey, GeneratePDF
- [x] Handler — 2026-06-25: POST /doctors/:id/keys, POST /prescriptions/:id/pdf com Swagger
- [x] Arquivo .http — 2026-06-25: references/http/09-documents.http

## Critério de conclusão

- Médico consegue cadastrar chave A1/A3
- PDF gerado e assinado com sucesso para prescrição emitida
- Domínio testado
