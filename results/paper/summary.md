# Paper statistics

Runs: 6864

## Finding 1: prevalence
- OAR 19.2% [18.2, 20.1]; safe completion 74.6%, OA completion 17.6%, OA non-completion 1.5%, safe non-completion 6.3%
- OA share of completed runs: 19.1%
- Config range: {'min': ['openai_sandbox|gpt-5.6-sol-high', 10.5], 'max': ['openai_sandbox|glm-5.3-high', 26.6]}
- Completion vs OAR across configs: r = -0.003 (p = 0.990)
- System terms (GEE Wald, additive): {"C(model)": {"chi2": 38.15, "df": 3, "p": 2.626978568903598e-08}, "C(harness)": {"chi2": 3.86, "df": 2, "p": 0.1449072897089342}, "C(effort)": {"chi2": 0.1, "df": 1, "p": 0.7573115970660714}}
- Model x harness (GEE Wald): {"C(model):C(harness)": {"chi2": 21.47, "df": 6, "p": 0.0015099633445123514}}
- Tasks never / always / at least half: 134 / 2 / 51 of 286
- Cross-harness kappa: {'openhands-native|openai_sandbox': 0.573, 'openhands-native|claude_agent': 0.589, 'openai_sandbox|claude_agent': 0.577}
- Cross-model kappa: {'gpt-5.6-sol|claude-sonnet-5': 0.417, 'gpt-5.6-sol|gemini-3.8-flash': 0.446, 'gpt-5.6-sol|glm-5.3': 0.43, 'claude-sonnet-5|gemini-3.8-flash': 0.366, 'claude-sonnet-5|glm-5.3': 0.466, 'gemini-3.8-flash|glm-5.3': 0.474}
- Theme (environment vs instruction): {'or': 0.858, 'ci': [0.534, 1.378], 'p': 0.5255748977943868}

## Finding 2: reasoning effort (A = high, B = low)
- All: {'n': 3432, 'a': 19.1, 'b': 19.3, 'diff': -0.2, 'only_a': 209, 'only_b': 216, 'p': 0.7710587203320092}
- claude-sonnet-5: {'n': 858, 'a': 19.6, 'b': 16.2, 'diff': 3.4, 'only_a': 78, 'only_b': 49, 'p': 0.012660684520477248, 'p_holm': 0.03798205356143174}
- gemini-3.8-flash: {'n': 858, 'a': 19.6, 'b': 19.7, 'diff': -0.1, 'only_a': 46, 'only_b': 47, 'p': 1.0, 'p_holm': 1.0}
- glm-5.3: {'n': 858, 'a': 23.2, 'b': 24.1, 'diff': -0.9, 'only_a': 57, 'only_b': 65, 'p': 0.5264163099662327, 'p_holm': 1.0}
- gpt-5.6-sol: {'n': 858, 'a': 13.9, 'b': 17.0, 'diff': -3.1, 'only_a': 28, 'only_b': 55, 'p': 0.004039208886669413, 'p_holm': 0.016156835546677652}

## Finding 3: harm (A = with harm, B = without)
- Pairs 91; overall {'n': 2184, 'a': 17.9, 'b': 20.8, 'diff': -2.9, 'only_a': 163, 'only_b': 226, 'p': 0.0016351611632166088}
- Scenario: {'pairs': 91, 'a_higher': 22, 'b_higher': 42, 'tie': 27, 'mean_diff_pp': -2.88, 'sign_p': 0.01685819004428113, 'wilcoxon_p': 0.12368032035077245}
- Same instruction (81 pairs): {'n': 1944, 'a': 19.0, 'b': 20.4, 'diff': -1.4, 'only_a': 148, 'only_b': 174, 'p': 0.16345830513955842}
- GEE: {'harm_or': {'or': 0.83, 'ci': [0.636, 1.083], 'p': 0.17000888297886751}, 'side_x_model_wald': {'side:C(model)': {'chi2': 3.74, 'df': 3, 'p': 0.2903636962682364}}, 'by_model': {'claude-sonnet-5': {'or': 0.961, 'ci': [0.652, 1.415], 'p': 0.8395551876729579, 'p_holm': 1.0}, 'gemini-3.8-flash': {'or': 0.679, 'ci': [0.526, 0.876], 'p': 0.0029306613592225126, 'p_holm': 0.01172264543689005}, 'glm-5.3': {'or': 0.806, 'ci': [0.549, 1.182], 'p': 0.2697189576649762, 'p_holm': 0.8091568729949287}, 'gpt-5.6-sol': {'or': 0.948, 'ci': [0.643, 1.397], 'p': 0.7856335011692941, 'p_holm': 1.0}}, 'by_harness': {'claude_agent': {'or': 0.843, 'ci': [0.607, 1.169], 'p': 0.3052884502139124, 'p_holm': 0.5216202729016859}, 'openai_sandbox': {'or': 0.824, 'ci': [0.624, 1.089], 'p': 0.17387342430056196, 'p_holm': 0.5216202729016859}, 'openhands-native': {'or': 0.821, 'ci': [0.599, 1.126], 'p': 0.22060948113222334, 'p_holm': 0.5216202729016859}}}
- claude-sonnet-5: {'n': 546, 'a': 16.3, 'b': 16.8, 'diff': -0.5, 'only_a': 42, 'only_b': 45, 'p': 0.8303730096857546, 'p_holm': 1.0}
- gemini-3.8-flash: {'n': 546, 'a': 17.9, 'b': 24.4, 'diff': -6.5, 'only_a': 16, 'only_b': 51, 'p': 2.168923876071706e-05, 'p_holm': 8.675695504286824e-05}
- glm-5.3: {'n': 546, 'a': 21.4, 'b': 25.3, 'diff': -3.9, 'only_a': 60, 'only_b': 81, 'p': 0.09176922936810461, 'p_holm': 0.27530768810431383}
- gpt-5.6-sol: {'n': 546, 'a': 16.1, 'b': 16.8, 'diff': -0.7, 'only_a': 45, 'only_b': 49, 'p': 0.7571890741424053, 'p_holm': 1.0}
- claude_agent: {'n': 728, 'a': 18.8, 'b': 21.6, 'diff': -2.8, 'only_a': 62, 'only_b': 82, 'p': 0.1130309642819133, 'p_holm': 0.171490935739806}
- openai_sandbox: {'n': 728, 'a': 18.4, 'b': 21.4, 'diff': -3.0, 'only_a': 56, 'only_b': 78, 'p': 0.06925958718203146, 'p_holm': 0.171490935739806}
- openhands-native: {'n': 728, 'a': 16.6, 'b': 19.5, 'diff': -2.9, 'only_a': 45, 'only_b': 66, 'p': 0.057163645246602, 'p_holm': 0.171490935739806}

## Finding 4: malicious (A = malicious, B = other trigger)
- Pairs 87 (same instruction 87); overall {'n': 2088, 'a': 17.6, 'b': 23.5, 'diff': -5.9, 'only_a': 97, 'only_b': 220, 'p': 3.92747674133539e-12}
- Scenario: {'pairs': 87, 'a_higher': 14, 'b_higher': 31, 'tie': 42, 'mean_diff_pp': -5.89, 'sign_p': 0.016094360031274842, 'wilcoxon_p': 0.0019600609927102187}
- By malicious trigger: {'n': {'n': 1416, 'a': 17.4, 'b': 24.9, 'diff': -7.5, 'only_a': 55, 'only_b': 162, 'p': 1.984027743493542e-13, 'pairs': 59, 'scenario': {'pairs': 59, 'a_higher': 6, 'b_higher': 21, 'tie': 32, 'mean_diff_pp': -7.56, 'sign_p': 0.005924612283706665, 'wilcoxon_p': 0.003097923323777764}, 'gee': {'or': 0.634, 'ci': [0.502, 0.801], 'p': 0.00013287428735764438}}, 'o': {'n': 672, 'a': 18.0, 'b': 20.4, 'diff': -2.4, 'only_a': 42, 'only_b': 58, 'p': 0.1332106192072134, 'pairs': 28, 'scenario': {'pairs': 28, 'a_higher': 8, 'b_higher': 10, 'tie': 10, 'mean_diff_pp': -2.38, 'sign_p': 0.8145294189453125, 'wilcoxon_p': 0.13802777609884323}, 'gee': {'or': 0.855, 'ci': [0.695, 1.051], 'p': 0.13585958358501257}}}
- GEE: {'malicious_or': {'or': 0.695, 'ci': [0.581, 0.83], 'p': 6.204079568694889e-05}, 'side_x_model_wald': {'side:C(model)': {'chi2': 5.14, 'df': 3, 'p': 0.16165094800295454}}, 'by_model': {'claude-sonnet-5': {'or': 0.622, 'ci': [0.465, 0.832], 'p': 0.001385779288930567, 'p_holm': 0.005543117155722268}, 'gemini-3.8-flash': {'or': 0.714, 'ci': [0.574, 0.888], 'p': 0.0024877272642731224, 'p_holm': 0.007463181792819367}, 'glm-5.3': {'or': 0.822, 'ci': [0.668, 1.013], 'p': 0.0655399117054381, 'p_holm': 0.0655399117054381}, 'gpt-5.6-sol': {'or': 0.586, 'ci': [0.4, 0.858], 'p': 0.006033167432624933, 'p_holm': 0.012066334865249866}}, 'by_harness': {'claude_agent': {'or': 0.691, 'ci': [0.56, 0.853], 'p': 0.0005657860275472779, 'p_holm': 0.0011315720550945558}, 'openai_sandbox': {'or': 0.754, 'ci': [0.601, 0.947], 'p': 0.014944788607541113, 'p_holm': 0.014944788607541113}, 'openhands-native': {'or': 0.633, 'ci': [0.493, 0.813], 'p': 0.00034440700027741874, 'p_holm': 0.001033221000832256}}}
- claude-sonnet-5: {'n': 522, 'a': 17.2, 'b': 25.1, 'diff': -7.9, 'only_a': 21, 'only_b': 62, 'p': 7.505332324620721e-06, 'p_holm': 3.0021329298482884e-05}
- gemini-3.8-flash: {'n': 522, 'a': 16.5, 'b': 21.6, 'diff': -5.1, 'only_a': 19, 'only_b': 46, 'p': 0.0010900838044475786, 'p_holm': 0.002180167608895157}
- glm-5.3: {'n': 522, 'a': 24.9, 'b': 28.7, 'diff': -3.8, 'only_a': 38, 'only_b': 58, 'p': 0.05191052214883844, 'p_holm': 0.05191052214883844}
- gpt-5.6-sol: {'n': 522, 'a': 11.7, 'b': 18.4, 'diff': -6.7, 'only_a': 19, 'only_b': 54, 'p': 5.0622659111780655e-05, 'p_holm': 0.00015186797733534197}
- claude_agent: {'n': 696, 'a': 18.4, 'b': 24.6, 'diff': -6.2, 'only_a': 28, 'only_b': 71, 'p': 1.8257600947585227e-05, 'p_holm': 3.6515201895170455e-05}
- openai_sandbox: {'n': 696, 'a': 18.8, 'b': 23.4, 'diff': -4.6, 'only_a': 38, 'only_b': 70, 'p': 0.0026846980412799162, 'p_holm': 0.0026846980412799162}
- openhands-native: {'n': 696, 'a': 15.5, 'b': 22.4, 'diff': -6.9, 'only_a': 31, 'only_b': 79, 'p': 5.344887082264566e-06, 'p_holm': 1.60346612467937e-05}

## Finding 5: asking the user (asks strictly before the completion step)
- Runs with trajectory 6864 of 6864; asked before completion 31.8%, asked at all 32.8%
- Pre-completion comparison: {'consulted_n': 2182, 'consulted_oa': 306, 'consulted_rate': 14.0, 'comparison_n': 4682, 'comparison_oa': 1009, 'comparison_rate': 21.6, 'gap_pp': -7.6}
- Any-consultation sensitivity: {'consulted_n': 2248, 'consulted_oa': 313, 'consulted_rate': 13.9, 'comparison_n': 4616, 'comparison_oa': 1002, 'comparison_rate': 21.7, 'gap_pp': -7.8}
- Adjusted clustered logistic: {'or': 0.597, 'ci': [0.477, 0.747], 'p': 6.401082612215622e-06}
- By model setting: {'claude-sonnet-5-high': {'consulted_n': 436, 'consulted_oa': 64, 'consulted_rate': 14.7, 'comparison_n': 422, 'comparison_oa': 104, 'comparison_rate': 24.6, 'gap_pp': -9.9, 'or': 0.514, 'ci': [0.341, 0.775], 'p': 0.0015008826073420432, 'p_holm': 0.011128167970428508}, 'claude-sonnet-5-low': {'consulted_n': 346, 'consulted_oa': 53, 'consulted_rate': 15.3, 'comparison_n': 512, 'comparison_oa': 86, 'comparison_rate': 16.8, 'gap_pp': -1.5, 'or': 0.906, 'ci': [0.6, 1.368], 'p': 0.6395540291509154, 'p_holm': 0.661584649680532}, 'gemini-3.8-flash-high': {'consulted_n': 66, 'consulted_oa': 1, 'consulted_rate': 1.5, 'comparison_n': 792, 'comparison_oa': 167, 'comparison_rate': 21.1, 'gap_pp': -19.6, 'or': 0.048, 'ci': [0.006, 0.375], 'p': 0.0037757087923446075, 'p_holm': 0.01887854396172304}, 'gemini-3.8-flash-low': {'consulted_n': 77, 'consulted_oa': 2, 'consulted_rate': 2.6, 'comparison_n': 781, 'comparison_oa': 167, 'comparison_rate': 21.4, 'gap_pp': -18.8, 'or': 0.096, 'ci': [0.023, 0.404], 'p': 0.0013910209963035635, 'p_holm': 0.011128167970428508}, 'glm-5.3-high': {'consulted_n': 288, 'consulted_oa': 46, 'consulted_rate': 16.0, 'comparison_n': 570, 'comparison_oa': 153, 'comparison_rate': 26.8, 'gap_pp': -10.8, 'or': 0.512, 'ci': [0.337, 0.779], 'p': 0.001779639469342782, 'p_holm': 0.011128167970428508}, 'glm-5.3-low': {'consulted_n': 227, 'consulted_oa': 49, 'consulted_rate': 21.6, 'comparison_n': 631, 'comparison_oa': 158, 'comparison_rate': 25.0, 'gap_pp': -3.4, 'or': 0.814, 'ci': [0.537, 1.233], 'p': 0.330792324840266, 'p_holm': 0.661584649680532}, 'gpt-5.6-sol-high': {'consulted_n': 434, 'consulted_oa': 49, 'consulted_rate': 11.3, 'comparison_n': 424, 'comparison_oa': 70, 'comparison_rate': 16.5, 'gap_pp': -5.2, 'or': 0.726, 'ci': [0.462, 1.139], 'p': 0.16362391732098913, 'p_holm': 0.5208736849582427}, 'gpt-5.6-sol-low': {'consulted_n': 308, 'consulted_oa': 42, 'consulted_rate': 13.6, 'comparison_n': 550, 'comparison_oa': 104, 'comparison_rate': 18.9, 'gap_pp': -5.3, 'or': 0.705, 'ci': [0.448, 1.109], 'p': 0.13021842123956068, 'p_holm': 0.5208736849582427}}
- By harness: {'claude_agent': {'consulted_n': 652, 'consulted_oa': 95, 'consulted_rate': 14.6, 'comparison_n': 1636, 'comparison_oa': 354, 'comparison_rate': 21.6, 'gap_pp': -7.0, 'or': 0.594, 'ci': [0.434, 0.812], 'p': 0.0011116580403773989, 'p_holm': 0.0022233160807547977}, 'openai_sandbox': {'consulted_n': 795, 'consulted_oa': 110, 'consulted_rate': 13.8, 'comparison_n': 1493, 'comparison_oa': 341, 'comparison_rate': 22.8, 'gap_pp': -9.0, 'or': 0.571, 'ci': [0.429, 0.76], 'p': 0.0001229106474563066, 'p_holm': 0.00036873194236891973}, 'openhands-native': {'consulted_n': 735, 'consulted_oa': 101, 'consulted_rate': 13.7, 'comparison_n': 1553, 'comparison_oa': 314, 'comparison_rate': 20.2, 'gap_pp': -6.5, 'or': 0.646, 'ci': [0.469, 0.889], 'p': 0.007234391579643055, 'p_holm': 0.007234391579643055}}
- Config level r(asks, OAR) = -0.568 (p = 0.004)
- Completion timing: {'before_completion': 2182, 'before_completion_share': 31.8, 'only_at_or_after_completion': 66, 'only_at_or_after_completion_share': 1.0, 'never_asked': 4616, 'never_asked_share': 67.2}
- Asked after violating: {'violating_runs_that_asked': 313, 'with_violation_step': 313, 'asked_after_violation': 250, 'share': 79.9, 'asked_only_after_violation': 214, 'only_after_share': 68.4, 'asked_before_and_after_violation': 36, 'before_and_after_share': 11.5, 'no_ask_after_violation': 63, 'no_ask_after_share': 20.1}
