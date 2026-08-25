/**
 * procurement_product_filter.js
 *
 * Dynamically filters the "product" dropdown on the ProcurementOrder admin
 * form based on whichever Purchase Order the operator selects.
 *
 * How it works:
 *  1. Django admin renders the purchase_order field as a Select2 autocomplete
 *     widget (because of autocomplete_fields = ['purchase_order']).
 *  2. We listen for the Select2 "select2:select" event on that widget.
 *  3. On selection, we call a lightweight JSON endpoint on the admin site
 *     (/admin/core/procurementorder/po-products/?po_id=<id>) which returns
 *     the products linked to that PO.
 *  4. We rebuild the product <select> with only those options so the operator
 *     cannot accidentally choose a product outside the PO.
 */

(function ($) {
    "use strict";

    /**
     * Rebuild the product dropdown with the given list of products.
     * @param {Array} products  - Array of {id, name} objects from the JSON API.
     * @param {jQuery} $product - The product <select> jQuery element.
     */
    function repopulateProductField(products, $product) {
        // Clear all current options
        $product.empty();

        if (products.length === 0) {
            // No products linked — show a placeholder so the operator knows
            $product.append(
                $("<option>", { value: "", text: "— No products linked to this PO —" })
            );
        } else {
            // Add a blank "select one" placeholder as the first option
            $product.append(
                $("<option>", { value: "", text: "---------" })
            );
            $.each(products, function (_, item) {
                $product.append(
                    $("<option>", { value: item.id, text: item.name })
                );
            });
        }

        // Trigger change so any other listeners (e.g. Select2 on product) notice
        $product.trigger("change");
    }

    /**
     * Reset the product dropdown to an "all products" state when no PO is selected.
     * The server-side queryset will do the definitive filtering on submit;
     * this just provides a clear visual cue.
     * @param {jQuery} $product
     */
    function resetProductField($product) {
        $product.empty();
        $product.append(
            $("<option>", { value: "", text: "— Select a Purchase Order first —" })
        );
        $product.trigger("change");
    }

    $(document).ready(function () {
        // Locate the purchase_order and product fields by their Django-generated IDs
        var $poField      = $("#id_purchase_order");
        var $productField = $("#id_product");

        if ($poField.length === 0 || $productField.length === 0) {
            // Not on the ProcurementOrder form — exit silently
            return;
        }

        /**
         * Fetch products for the given PO ID and repopulate the dropdown.
         * The endpoint is registered on the admin site via get_urls() in admin.py.
         */
        function fetchAndUpdateProducts(poId) {
            if (!poId) {
                resetProductField($productField);
                return;
            }

            $.getJSON(
                // Endpoint served by ProcurementOrderAdmin.get_urls()
                "/admin/core/procurementorder/po-products/",
                { po_id: poId },
                function (data) {
                    repopulateProductField(data.products, $productField);
                }
            ).fail(function () {
                console.error("Failed to fetch products for PO ID:", poId);
                resetProductField($productField);
            });
        }

        // ── Initial state ──────────────────────────────────────────────────────
        // If editing an existing record the PO field already has a value;
        // trigger a fetch so the product dropdown shows only the linked products.
        var initialPoId = $poField.val();
        if (initialPoId) {
            fetchAndUpdateProducts(initialPoId);
        } else {
            resetProductField($productField);
        }

        // ── Live update on Select2 selection ──────────────────────────────────
        // Django's autocomplete widget fires "select2:select" when the user picks a PO.
        $poField.on("select2:select", function (e) {
            fetchAndUpdateProducts(e.params.data.id);
        });

        // Also handle clearing the PO selection
        $poField.on("select2:clear select2:unselect", function () {
            resetProductField($productField);
        });

        // Fallback: plain "change" event for environments without Select2
        $poField.on("change", function () {
            // Avoid double-firing when Select2 is active (it fires its own events above)
            if (!$(this).hasClass("select2-hidden-accessible")) {
                fetchAndUpdateProducts($(this).val());
            }
        });
    });

}(django.jQuery));
