import unittest

from backend.recipe_manager import RecipeManager


class RediscoverPhaseOneRecipeTests(unittest.TestCase):
    def test_analysis_inputs_are_substituted_into_new_style_prompt(self):
        manager = RecipeManager("recipes")
        inputs = {
            "tracks_found": 53,
            "top_genres": '{"Rock": 17, "Pop": 9}',
            "top_artists": '{"Artist A": 8}',
            "top_decades": '{"2000": 12}',
            "avg_play_count": 3.4,
            "available_genres": '["Rock", "Pop"]',
        }

        recipe = manager.apply_recipe("re_discover_phase1_v2", inputs)
        prompt = recipe["model_instructions"]

        self.assertIn("**Tracks analyzed**: 53", prompt)
        self.assertIn('{"Rock": 17, "Pop": 9}', prompt)
        self.assertIn('{"Artist A": 8}', prompt)
        self.assertIn('{"2000": 12}', prompt)
        self.assertIn("Average play count**: 3.4", prompt)
        self.assertIn('["Rock", "Pop"]', prompt)
        for key in inputs:
            self.assertNotIn("{{" + key + "}}", prompt)


if __name__ == "__main__":
    unittest.main()
