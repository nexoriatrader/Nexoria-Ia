# NEXORA INTELLIGENCE 2.0 — I05 Learning Engine (pesquisa pública)

**Branch isolada:** `feature/intelligence20-i05-learning-research`.
**Base imutável:** I04 / PR #54, SHA
`0d2aac586d9294068cf8c1501712367947ced603`.
**Estado de liberação:** PR DRAFT até CI no SHA final.
**Sem conexão a ordens financeiras, pagamento, compra, venda, stop, alteração
de estratégias ou execução de modelos sobre contas DEMO/REAL.**

## 1. Integração real, sem arquitetura paralela

`learning_engine.collect_public_history` usa uma única consulta
`market_data.read_closed_window` (I01) de 1000 candles públicos
**efetivamente fechados**, sem novo WebSocket ou cliente Deriv. Cada
janela histórica com 72 candles já fechados passa pelo Context Engine
**real** da I03, sem look-ahead, produzindo o histórico de regimes que
alimentará o treino. Só conhecimentos MARKET `ACTIVE`, íntegros,
dentro da validade, com regime I03 atual compatível, são aceitos via
I04 `get_knowledge`. Ausência de fonte válida produz recusa explícita.

`evaluate_public_regimes` treina um estimador simples e auditável de
**transições de rótulos de regime de mercado** (`REGIME_MARKOV_LAPLACE_V1`).
Não há treinamento de estratégia financeira, sinal de entrada,
previsão de lucro, dados de usuário ou predição de preço. O artefato
é composto de contagens de transições suavizadas e é reavaliado
deterministicamente antes de ser considerado para aprovação.

## 2. Treinamento/validação fora da amostra

- Histórico fechado: **1000** candles por fonte, com validação I01/I03
  de completude, timestamps, gaps, idade e tipos.
- Treinamento cronológico: primeiros **64%** dos rótulos I03 (>=500
  transições), sem shuffle. **Embargo de 72 candles** entre treino e
  holdout, igual ao tamanho da janela do classificador I03.
- Holdout temporal final: pelo menos **200** classificações, inclusive
  duas metades temporais comparadas separadamente.
- Baseline: persistência ingênua (`regime seguinte = regime atual`).
  Challenger só passa quando a acurácia supera o baseline em >=4 pontos
  percentuais, o limite inferior de Wilson 95% supera o baseline em
  >=1 ponto percentual, e **ambas** as metades têm lift >=2 pontos
  percentuais. Acurácia é de **rótulos de regime**, não rentabilidade.
- Dado incompleto, amostra curta, modelo incoerente, OOS fraco,
  regime desconhecido ou comportamento irregular levam a rejeição,
  `NO_VALIDATED_RESEARCH_MODEL` ou manutenção do champion anterior.
- A escolha de `M1/M5/M15/H1` é restrita aos intervalos suportados
  pela I01. O ciclo programado avalia **um** conhecimento de mercado
  público por execução, minimizando carga e concorrência.

## 3. Persistência, champions e rastreabilidade

**Sem novos bancos/tabelas/caches:** os candidatos `MODEL` e
evidências vivem nas **mesmas** tabelas I04. O artefato reprodutível e
o release log imutável são armazenados em
`HistoricalResearchReport`, existente, com chaves
`I05_ART_...`, `I05_REL_...` e `I05_EVT_...`.
Retenção geral não apaga os registros auditáveis I05.

- **Challenger:** criado pelo ciclo com estado I04
  `UNDER_VALIDATION`, evidências públicas separadas por período de
  treino e holdout. Nunca é promovido apenas pela descoberta.
- **VALIDATED (pesquisa):** método interno I05
  `validate_research_challenger` recalcula o artefato OOS, verifica
  a integridade I04/CAS, a suficiência e a separação temporal de
  evidências. A aprovação exige também
  `PUBLIC_INDEPENDENT_VALIDATION`: uma SEGUNDA janela pública
  posterior ao primeiro holdout e a outro embargo de 72 candles,
  com pelo menos 200 classificações **novas**, sem retreinamento.
  Essa janela precisa superar o baseline, Wilson e a comparação
  das duas metades temporais. O ciclo programado verifica até um
  challenger pendente por execução, mas não o promove automaticamente.
  A I04 continua proibindo a promoção genérica MODEL.
- **Champion:** somente `promote_research_champion`, com flag
  explícita de promoção, aprovação auditável, modelo VALIDATED,
  release esperado (CAS), bloqueio global caso exista qualquer contrato
  DEMO/REAL aberto e lock transacional PostgreSQL.
  Essa promoção altera APENAS a seleção de modelos de **pesquisa**
  (não altera uma estratégia, ordem, sinal ou permissão financeira).
- **Fallback:** o champion anterior permanece um modelo de pesquisa
  validado e pode ser escolhido como fallback no registro de release.
  Sem fallback válido, o resultado seguro é
  `NO_VALIDATED_RESEARCH_MODEL`.
- **Integridade temporal:** decisões I04/I05 rejeitam alterações
  retroativas e registros com decisão datada no futuro. Uma promoção
  nunca pode anteceder a evidência que a sustenta.
- **Monitoramento:** `monitor_and_rollback` exige >=200 novas
  observações públicas fora da janela de treinamento/release,
  nunca conta o mesmo fechamento duas vezes. Duas janelas **distintas**
  com degradação acentuada levam a `AUTO_ROLLBACK` do ponteiro de
  pesquisa para o fallback, ou a nenhum modelo se ele estiver
  inválido. A trilha SHA256 encadeia gerações, justificativas,
  timestamps e anterioridade. Nenhuma estratégia é modificada.
- **Revisão:** modelos I04 fora de validade, revisão pendente ou com
  integridade quebrada são excluídos imediatamente, mesmo se houver
  release que os referencie.

## 4. Treinamento programado e por eventos

Reuso do `research_persistence.research_loop` **já existente**
(não há segundo scheduler). O ciclo público I05 é habilitado
separadamente e só roda se I04 também estiver habilitada:

```text
NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED=false
NEXORA_INTELLIGENCE_LEARNING_ENABLED=false
NEXORA_INTELLIGENCE_LEARNING_PROMOTION_ENABLED=false
```

O ciclo programado é devido a cada 24 horas. Alterações confirmadas
de regime público que invalidam conhecimento I04 registram evento
persistente `I05_EVT_` e antecipam a próxima avaliação. O processamento
é limitado a uma fonte PUBLIC MARKET por ciclo, com auditoria do motivo
de recusa, sem execução de ordens nem treino de dados privados.
Se uma mudança invalidar a única fonte conhecida, o ciclo registra
`SKIPPED_NO_VALIDATED_MARKET_SOURCE` até que um novo conhecimento
seja validado — **não contorna** a I04.

A promoção é SEMPRE separada do ciclo automático e começa OFF.
A avaliação automática posterior pode apenas monitorar e reverter
a seleção de pesquisa; nunca promove novo modelo por conta própria.

## 5. API pública autenticada, somente leitura

`GET /api/research/learning-status?market=derived&symbol=R_100&timeframe_seconds=60`

Retorna versão, estado de release, id de modelo de pesquisa e fallback,
ou `NO_VALIDATED_RESEARCH_MODEL`. Quando a I05 está OFF, responde
503; sem sessão autenticada, 401. Não existe endpoint de escrita,
promoção, compra ou execução. DEMO/REAL consultam a mesma seleção
pública sem qualquer identificador financeiro nos modelos.

## 6. Merge e deploy progressivos: estado real

As PRs #45–#54 ainda estão DRAFT e stacked. A `main` em
`d51aedba27458057e2803346da85f887fd7aede9` é 231 commits
ancestral da I04 (diferença inclui P0/F01, backend financeiro,
migrações formais). A PR de release candidata **#55** tem base
`main`, permanece DRAFT, com testes de compatibilidade de I01–I04,
sequência de backup, ensaio, deploy, rollback e flags.

NÃO haverá merge da cadeia ou deploy sem confirmação dos gates F01,
compatibilidade do PostgreSQL e identificação do workspace/serviço
Render. I04 requer migração formal PostgreSQL aprovada
**separadamente**. I05 não adiciona migração própria e não deverá ser
ativada antes da I04. Mesmo com I04 e I05 ativas, MODELOS DE PESQUISA
não podem entrar em execução financeira.

Ver `docs/INTELLIGENCE_20_RELEASE_SEQUENCE.md` na PR #55 para
etapas R0–R3 e procedimento de backup/rollback.

## 7. Critérios de teste

`tests/test_intelligence2_learning_engine.py` cobre treino
cronológico, embargo, holdout, baseline, dados falsos/privados,
descoberta/modelo I04 persistente, integridade de artefato, CAS de
release, bloqueio por operações abertas, champion/challenger/fallback,
rollback por janelas distintas, eventos persistentes e deduplicados,
ciclo do scheduler existente, retenção e nenhuma compra.

Testes PostgreSQL 16 descartáveis I05 verificam promoção concorrente
sem duas vitórias e persistência/releitura, mantendo todos os testes
P0/F01/I01–I04. Compromisso de entrega: CI completo no SHA final,
PR DRAFT, sem merge/deploy/DDL de produção.

**Limitação explícita:** esta I05 é um Learning Engine real de
classificação estatística de **regimes públicos para pesquisa**,
não uma evidência de lucro ou uma funcionalidade liberada para
estratégias financeiras. O caminho de produção ficará condicionado
à consolidação segura, e nenhuma inteligência financeira autônoma
será ativada implicitamente.
