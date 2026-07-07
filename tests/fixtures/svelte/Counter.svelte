<script context="module">
  export const prerender = true;
</script>

<script lang="ts">
  import Display from "./Display.svelte";
  import { onMount } from "svelte";

  interface Props {
    label: string;
  }

  // Svelte 5 runes
  let count = $state(0);
  let doubled = $derived(count * 2);

  // Svelte 4 props
  export let label;

  // Svelte 4 reactive label
  $: quadrupled = doubled * 2;

  function increment(): void {
    count += 1;
  }

  onMount(() => increment());
</script>

<!-- <Ghost /> lives in a comment and must not become an edge -->
<main>
  <h1>{label}</h1>
  <Display value={count} />
  <UserBadge />
  <button on:click={increment}>+{quadrupled}</button>
</main>
