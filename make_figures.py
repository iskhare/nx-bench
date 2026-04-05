"""Generate figures for the report."""

import json
import matplotlib.pyplot as plt
import matplotlib
import numpy as np

matplotlib.rcParams.update({
    'font.size': 11,
    'font.family': 'serif',
    'axes.labelsize': 12,
    'axes.titlesize': 12,
    'xtick.labelsize': 10,
    'ytick.labelsize': 10,
    'legend.fontsize': 10,
    'figure.dpi': 300,
})

data = json.load(open('results/all_results.json'))

models = {
    'openai/gpt-5.4-mini': 'GPT-5.4-mini',
    'openai/gpt-5-codex': 'GPT-5-codex',
    'openai/gpt-5.1-codex-mini': 'GPT-5.1-codex-mini',
}

colors = {
    'GPT-5.4-mini': '#4C72B0',
    'GPT-5-codex': '#DD8452',
    'GPT-5.1-codex-mini': '#55A868',
}

by_model = {}
for r in data:
    label = models[r['model']]
    by_model.setdefault(label, []).append(r['score'])

# Score distribution histogram
fig, ax = plt.subplots(figsize=(6, 3.5))
bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
width = 0.025
offsets = [-width, 0, width]

for i, (label, scores) in enumerate(by_model.items()):
    counts, _ = np.histogram(scores, bins=bins)
    centers = [(bins[j] + bins[j+1]) / 2 + offsets[i] for j in range(len(bins)-1)]
    ax.bar(centers, counts, width=width, label=label, color=colors[label], alpha=0.85, edgecolor='white', linewidth=0.5)

ax.set_xlabel('Score')
ax.set_ylabel('Number of Tasks')
ax.set_title('Score Distribution Across Models')
ax.legend(loc='upper left')
ax.set_xlim(-0.05, 1.1)
ax.set_xticks([0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
plt.tight_layout()
plt.savefig('figures/score_distribution.pdf', bbox_inches='tight')
plt.savefig('figures/score_distribution.png', bbox_inches='tight')
print("Saved score_distribution")

# Per-category grouped bar chart
tasks = {t['task_id']: t for t in json.load(open('tasks.json'))}
categories = ['bugfix', 'feature', 'performance', 'refactor']
cat_labels = ['Bugfix', 'Feature', 'Performance', 'Refactor']

fig, ax = plt.subplots(figsize=(6, 3.5))
x = np.arange(len(categories))
width = 0.22

for i, (label, scores_list) in enumerate(by_model.items()):
    cat_means = []
    for cat in categories:
        cat_scores = [r['score'] for r in data if models[r['model']] == label
                      and tasks.get(r['task_id'], {}).get('category') == cat]
        cat_means.append(np.mean(cat_scores) if cat_scores else 0)
    ax.bar(x + (i - 1) * width, cat_means, width, label=label, color=colors[label], alpha=0.85, edgecolor='white', linewidth=0.5)

ax.set_ylabel('Mean Score')
ax.set_title('Performance by Task Category')
ax.set_xticks(x)
ax.set_xticklabels(cat_labels)
ax.legend(loc='lower right')
ax.set_ylim(0, 1.1)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
plt.tight_layout()
plt.savefig('figures/category_scores.pdf', bbox_inches='tight')
plt.savefig('figures/category_scores.png', bbox_inches='tight')
print("Saved category_scores")
