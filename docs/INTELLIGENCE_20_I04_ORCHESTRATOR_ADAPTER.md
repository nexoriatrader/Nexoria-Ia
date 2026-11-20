# I04 Knowledge Base ↔ Research Orchestrator — integração isolada

**Branch:** `feature/intelligence20-i04-research-orchestrator-adapter`  
**Base:** PR #60 (`feature/intelligence20-research-orchestrator-i01-i06`), que preserva a PR #59 — I06 Research Evidence Engine.  
**Status:** pesquisa pública, sem merge, deploy, migração ou autorização financeira.

## Interfaces realmente disponíveis

I04 já está implementada em **outra branch**, `feature/intelligence20-i04-knowledge-base`, e NÃO integra ainda a PR #60.

O adaptador usa os contratos públicos I04 existentes, sem inventar métodos:
- `knowledge_base.enabled()` — flag `NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED`, OFF por padrão;
- `knowledge_base.retrieve_active(category, market, symbol, observed_regime, now, limit)` — busca exclusivamente por `MARKET` ou `STRATEGY`, devolve listas `active` e `excluded`;
- `knowledge_base.get_knowledge(id, now, observed_regime, include_lineage=True)` — recupera registro público, histórico de decisões e evidências já validadas.

A I04 original continua dona do armazenamento e das regras transacionais:
`db.SessionLocal`, `IntelligenceKnowledge`, `IntelligenceKnowledgeEvidence` e `IntelligenceKnowledgeDecision`. Nenhum desses modelos, arquivos de I04, tabelas, migrações ou caches foram copiados ou duplicados neste adaptador.

A PR #60 usa `orchestrate_public_research()`, que chama I01→I02→I03→I06 uma única vez. A nova função `orchestrate_research_with_i04()` executa essa cadeia existente uma vez, seguida da busca limitada na I04 em thread de trabalho; **não cria novo scan, feed, cache, endpoint ou processo persistente**.

## Correlação pública

O orquestrador passou a expor apenas dois dados **já disponíveis** na I06/I02/I03: `strategy_family` de I02 e `research_timeframes_seconds` verificados de I03. Não os calcula novamente, e não inclui dados de usuário.

Para ativos com consenso I03, `observed_trend=UP` corresponde ao regime público `UPTREND` e `DOWN` a `DOWNTREND` para consulta I04. Sem consenso ou hierarquia válida, a I04 não é consultada.

Consulta por ativo + mercado + regime, em categorias `MARKET` e, quando identificada a família de estudo, `STRATEGY`. A própria API da I04 não recebe filtro de timeframe ou família; por isso o adaptador **filtra depois da recuperação**, exigindo que o timeframe do registro seja 0 (público genérico) ou um dos timeframes já verificados da I03. Em STRATEGY, a família armazenada deve corresponder exatamente à família pública da I02.

## Revalidação antes de usar

A busca `retrieve_active()` da I04 verifica assinatura, cadeia de decisões, `state=ACTIVE`, validade, prazo de revisão, categoria, ativo, mercado e regime. O adaptador chama também `get_knowledge(..., include_lineage=True)` e reexamina:

- Mesmo ID, revisão, categoria, mercado, símbolo e regime, sem divergência entre as duas leituras;
- `valid_from <= now < min(valid_until, next_review_at)`; nenhuma leitura com revisão vencida é reaproveitada;
- `reusable_for_research=true`, `reuse_status=CURRENT_VERIFIED_ACTIVE`, permissões financeiras permanentemente falsas;
- Digest público SHA-256 presente e histórico de decisões concluindo em `ACTIVE`, revisão idêntica;
- Pelo menos duas origens independentes com evidência pública `verified=true` e pelo menos 72 observações cada (a I04 permanece autoridade sobre a suficiência e a integridade completas);
- Regime e direção não contraditórios; na categoria STRATEGY, mesma família I02; sem campos privados.

Qualquer conhecimento inválido, divergente, expirado ou com revisão vencida é **excluído da contribuição** e descrito apenas por ID/categoria/código de motivo. O adaptador **não reativa, renova, promove, altera ou invalida** registros I04. Renovação/revalidação efetiva de registros antigos continua exigindo o fluxo I04 existente: revisão formal, novas evidências independentes, transições CAS e aprovação separada.

## Rastreabilidade e influência

Cada ativo recebe `knowledge_base` com status, contagem consultada, lista minimizada de IDs/revisões válidos, origem, digest público, prazo de validade/revisão e `contribution=PUBLIC_CONTEXT_CORROBORATION`. Evidências detalhadas, payloads originais, saldos e informações de contas **não** são copiados ao relatório.

Conhecimento público válido pode **corroborar** um contexto técnico já produzido pela I06, mas nunca aumentar a pontuação de completude, promover sozinho um ativo a `RESEARCH_READY`, distribuir sinais ou autorizar trades. Contradição explícita de direção entre uma I04 ACTIVE verificada e a tendência pública I03 reduz `RESEARCH_READY` para `WAIT_RECHECK` com justificativa auditável. Falha de integridade, DB/schema indisponível ou consulta parcial descarta todas as contribuições da consulta afetada (fail closed).

Limites: no máximo 8 ativos, 2 categorias, 8 IDs por categoria na busca e até 4 detalhes por categoria. Excesso identificado resulta em estado de rechecagem, sem inventar cobertura.

## Contrato de implementação

`app/intelligence2/i04_research_adapter.py` fornece:

- `enrich_research_with_i04(report, now=..., provider=None)` — leitura e associação sobre relatório já consolidado da PR #60;
- `orchestrate_research_with_i04(previous=..., prior_i06=..., expiry_minutes=..., now=..., provider=None)` — coordenação assíncrona reutilizando a cadeia única;
- O argumento `provider` injeta **nos testes** um provedor com o contrato real I04. No uso normal, é feita importação tardia de `app.intelligence2.knowledge_base` e verificação de `enabled()`.
- `NOT_CONNECTED` se a I04 ainda não está integrada, `DISABLED` com flag OFF, `I04_UNAVAILABLE` caso a leitura falhe, sem dados validados indevidamente.

O módulo não possui rota HTTP própria, segundo armazenamento, scheduler, DDL, execução financeira, publicação nem qualquer alteração de configuração. A Intelligence em produção segue desligada.

## Dependência restante

**A integração de leitura está implementada e testável contra o contrato de I04, mas ainda não é integração de banco funcional em produção.** A I04 vive em outra branch e seu esquema exige a migração formal de I04, não autorizada nesta missão. Não foi incorporado o código nem executada a migração de I04. Quando I04 for integrada de modo independente e seu esquema formal estiver disponível, o mesmo adaptador poderá utilizar os registros compartilhados sem reconstruir a Knowledge Base.

Os testes desta PR usam somente dados públicos sintéticos com o mesmo esquema de respostas da I04, além da I06 e do orquestrador reais em memória. PostgreSQL de produção não é acessado.

**PR em DRAFT. Sem merge, deploy, migrações ou alteração das flags REAL.**
