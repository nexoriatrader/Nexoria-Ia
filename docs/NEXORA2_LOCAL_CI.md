# NEXORA 2.0 — validação sem franquia do GitHub Actions

A conta atingiu os minutos gratuitos do GitHub Actions. Isso **não é um resultado de teste de código**: workflows sem executor e sem etapas não validam nem reprovam a PR.

O executor `scripts/run_nexora2_offline_ci.py` permite executar verificações em um checkout isolado da branch de integração, **sem contratar minutos, fazer deploy ou acessar produção**.

## Pré-requisitos

- Um computador ou ambiente isolado com Python 3.12 ou versão compatível;
- checkout autenticado do repositório privado na branch `feature/nexora2-integrated-workspace-20261121`;
- dependências instaladas com `python -m pip install -r requirements.txt`.

Não configure nem copie tokens de corretora, senhas de banco de produção ou chaves reais.

## Execução

Da raiz da cópia do repositório:

```bash
python scripts/run_nexora2_offline_ci.py --mode focused
python scripts/run_nexora2_offline_ci.py --mode full --report /tmp/nexora2-local-ci.json
```

O modo `focused` testa o painel NEXORA 2.0, o histórico pessoal, a pesquisa OHLC, a comparação e o Context Engine. O modo `full` inclui toda a suíte Python. Ambos incluem compilação, guarda de diffs, scanner dos arquivos atuais e auditoria de credenciais no histórico Git alcançável (é obrigatório usar clone completo).

Para inspecionar os comandos sem executar testes:

```bash
python scripts/run_nexora2_offline_ci.py --mode full --dry-run
```

**Não execute isso no serviço público.** Use apenas um checkout local isolado. O executor cria uma base SQLite temporária, uma chave Fernet descartável, descarta as credenciais e os parâmetros de banco/execução herdados do host, e força os flags financeiros a `false`.

## Interpretação dos resultados

O relatório JSON opcional registra as verificações, os códigos de saída e a duração. `passed=true` só é emitido após todas as etapas selecionadas retornarem sucesso. Uma falha interrompe o fluxo.

Os testes do próprio executor foram verificados em ambiente isolado, mas **a suíte completa deste repositório ainda não foi executada fora do GitHub Actions**.

Uma execução local aprovada **não substitui**:

1. Testes de compatibilidade, migração formal e zero DDL com PostgreSQL descartável;
2. Regressões completas de autenticação, isolamento entre usuários e concorrência;
3. Revisão de código e liberação independente de produção.

A `main` deve permanecer preservada até essas etapas terminarem. O módulo DEMO existente continua separado, sem mudanças operacionais; as proteções REAL continuam vigentes.

## Conferência offline de um relatório histórico exportado

O botão **BAIXAR RELATÓRIO JSON** exporta a entrada OHLC e o relatório
analítico completo, com evidências por módulo (não somente o resumo da tela).
Após obter o arquivo, é possível verificar sua integridade sem internet,
sem bibliotecas externas, sem consultar o PostgreSQL e sem credenciais:

```bash
python scripts/verify_nexora2_export.py caminho/para/nexora-pesquisa-123.json
```

A mensagem `internal_checksums_match: true` significa somente que os
**checksums SHA-256 internos do arquivo são consistentes**. Os hashes **não**
constituem assinatura digital, não garantem autoria ou imutabilidade contra
quem puder substituir o arquivo e seus hashes, **não verificam a origem das
cotações**, e não são previsões ou resultados financeiros.

O validador rejeita arquivos truncados, formatos incompatíveis, JSON com
chaves repetidas, valores não finitos e dados ou evidências modificados.
Ele não chama a corretora, não executa ordens e não altera arquivos.

## Antes de considerar visibilidade pública

**Não torne público o repositório inteiro apenas porque os 2.000 minutos de
Actions foram consumidos.** A publicação também expõe o histórico de commits
e pode tornar públicos logs de workflows anteriores.

Execute em um clone **não superficial**, com todas as referências relevantes
obtidas do GitHub:

```bash
python3 scripts/check_repository_security.py
python3 scripts/check_repository_history.py
```

Um resultado limpo detecta somente padrões conhecidos. Ele NÃO certifica a
ausência de credenciais opacas, segredos em refs apagadas, anexos, logs ou
sistemas externos. Verifique estes materiais antes de publicar qualquer código.
Se houver segredo comprometido, é necessário revogar/rotacionar credenciais;
apagá-las do HEAD não basta. A `main` de produção não deve ser alterada como
parte dessas verificações.

